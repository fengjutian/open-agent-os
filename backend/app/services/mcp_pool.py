"""MCP 连接池。

为什么之前是「每次调用起一个进程」：简单可靠。代价是每次工具调用都要付一次
进程启动 + 握手，高频调用时很明显。

池化的两个关键约束：

1. **一个连接同一时刻只能被一个调用使用**。stdio 上的 JSON-RPC 是有状态的单通道，
   两个并发请求会让响应交错。因此按服务器名加锁**串行化**，而不是维护一个
   无序的「可用连接队列」。
2. **出过协议错误的连接必须丢弃**。stdin/stdout 的帧一旦错位，后续响应可能永远
   对不上，复用只会把错误扩散给下一个调用。

空闲超过 `idle_seconds` 的连接在下次获取时回收，避免长期占着子进程。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from app.core.config import McpServerConfig
from app.services.mcp_client import McpError, McpStdioClient

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    client: McpStdioClient | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)


class McpPool:
    def __init__(self, *, idle_seconds: float = 60.0) -> None:
        self._idle_seconds = idle_seconds
        self._entries: dict[str, _Entry] = {}
        self._guard = asyncio.Lock()
        # 测试用：统计真实启动次数
        self.spawn_count = 0

    async def _entry(self, server_name: str) -> _Entry:
        async with self._guard:
            entry = self._entries.get(server_name)
            if entry is None:
                entry = _Entry()
                self._entries[server_name] = entry
            return entry

    @staticmethod
    def _is_alive(client: McpStdioClient | None) -> bool:
        if client is None:
            return False
        process = getattr(client, "_process", None)
        return process is not None and process.returncode is None

    @asynccontextmanager
    async def session(
        self, server_name: str, config: McpServerConfig, *, timeout: float
    ) -> AsyncIterator[McpStdioClient]:
        entry = await self._entry(server_name)

        async with entry.lock:
            stale = (
                entry.client is not None
                and (
                    not self._is_alive(entry.client)
                    # 用 >= 让 idle_seconds=0 表达「不保留空闲连接」
                    or time.monotonic() - entry.last_used >= self._idle_seconds
                )
            )
            if stale:
                logger.info("mcp connection recycled", extra={"fields": {"server": server_name}})
                await self._close_quietly(entry.client)
                entry.client = None

            if entry.client is None:
                client = McpStdioClient(
                    command=config.command,
                    args=config.args,
                    env=config.env,
                    # 读超时作为兜底，比整体预算宽松，让外层 wait_for 先触发
                    timeout=timeout + 5.0,
                )
                self.spawn_count += 1
                try:
                    await asyncio.wait_for(client.start(), timeout=timeout)
                except Exception:
                    await self._close_quietly(client)
                    raise
                entry.client = client

            client = entry.client
            try:
                yield client
            except McpError:
                # 协议帧可能已错位：丢掉连接，下次重连
                logger.warning(
                    "mcp connection dropped after protocol error",
                    extra={"fields": {"server": server_name}},
                )
                await self._close_quietly(client)
                entry.client = None
                raise
            finally:
                entry.last_used = time.monotonic()

    async def reset(self, server_name: str) -> None:
        """丢弃某个服务器的连接（例如调用超时后，响应可能还在路上）。"""
        entry = await self._entry(server_name)
        async with entry.lock:
            if entry.client is not None:
                await self._close_quietly(entry.client)
                entry.client = None

    async def close_all(self) -> None:
        async with self._guard:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            await self._close_quietly(entry.client)

    @staticmethod
    async def _close_quietly(client: McpStdioClient | None) -> None:
        if client is None:
            return
        try:
            await asyncio.shield(client.close())
        except Exception:  # pragma: no cover - 关闭失败不该影响调用方
            logger.debug("mcp close failed", exc_info=True)


# 进程范围内共享一个池；关闭时由 lifespan 统一回收
_pool: McpPool | None = None


def get_mcp_pool() -> McpPool:
    global _pool
    if _pool is None:
        from app.core.config import settings

        _pool = McpPool(idle_seconds=settings.mcp_idle_seconds)
    return _pool

"""最小 MCP 客户端（stdio 传输）。

MCP 的 stdio 传输是「每行一个 JSON-RPC 2.0 消息」，所以这里只需处理换行分帧
和按 id 匹配响应。刻意保持最小：

- 只实现本项目用到的 `tools/list` 与 `tools/call`；
- 每次调用起一个进程、用完即关（简单可靠；进程池是后续的性能优化）；
- 读超时在客户端兜底，整体超时由上层用 `asyncio.wait_for` 控制；
- 服务器 stderr 直接丢弃，避免管道写满导致死锁。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
_CLOSE_TIMEOUT = 5.0


class McpError(Exception):
    """MCP 协议层或传输层错误。"""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class McpCallResult:
    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False

    def text(self) -> str:
        parts: list[str] = []
        for item in self.content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)


class McpStdioClient:
    def __init__(
        self,
        *,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._command = command
        self._args = list(args or [])
        self._env = dict(env or {})
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self.server_info: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def __aenter__(self) -> McpStdioClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> None:
        environment = {**os.environ, **self._env} if self._env else None
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )
        except OSError as exc:
            raise McpError(f"无法启动 MCP 服务器 {self._command}: {exc}") from exc

        await self._initialize()

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return

        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=_CLOSE_TIMEOUT)
        except TimeoutError:
            logger.warning("MCP server did not exit in time, killing it")
            process.kill()
            await process.wait()

    async def _initialize(self) -> None:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-os", "version": "0.1"},
            },
        )
        self.server_info = result.get("serverInfo") or {}
        await self._notify("notifications/initialized")

    # ------------------------------------------------------------------
    # 能力
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[McpTool]:
        result = await self._request("tools/list", {})
        raw_tools = result.get("tools") or []
        tools: list[McpTool] = []
        for raw in raw_tools:
            name = raw.get("name")
            if not name:
                continue
            tools.append(
                McpTool(
                    name=str(name),
                    description=str(raw.get("description") or ""),
                    input_schema=raw.get("inputSchema") or {"type": "object"},
                )
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content")
        if not isinstance(content, list):
            content = [{"type": "text", "text": "" if content is None else str(content)}]
        return McpCallResult(content=content, is_error=bool(result.get("isError")))

    # ------------------------------------------------------------------
    # 传输
    # ------------------------------------------------------------------

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id = self._next_id + 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

        while True:
            message = await self._read_message()
            if message.get("id") != request_id:
                # 服务器主动推送的通知，忽略即可
                continue
            error = message.get("error")
            if error:
                raise McpError(
                    str(error.get("message") or "MCP 返回错误"),
                    code=error.get("code") if isinstance(error.get("code"), int) else None,
                )
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError("MCP 连接未建立")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        process.stdin.write(line.encode("utf-8"))
        await process.stdin.drain()

    async def _read_message(self) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise McpError("MCP 连接未建立")

        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=self._timeout)
        except TimeoutError as exc:
            raise McpError("等待 MCP 响应超时") from exc

        if not line:
            raise McpError("MCP 服务器已关闭输出")
        try:
            text = line.decode("utf-8") if isinstance(line, bytes) else line
        except UnicodeDecodeError as exc:
            # MCP 的 stdio 传输要求 UTF-8；Windows 上 Python 子进程默认 cp936，
            # 这类问题会以「看不懂的字节」出现，这里给出可诊断的错误。
            raise McpError("MCP 返回了非 UTF-8 数据（服务器可能没有设置输出编码）") from exc
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpError("MCP 返回了非法 JSON") from exc
        if not isinstance(message, dict):
            raise McpError("MCP 返回的消息不是对象")
        return message

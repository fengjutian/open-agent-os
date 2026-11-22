"""工具执行。

关键安全性质：**注册一个 ToolDefinition 不会产生任何执行能力**。
只有名字命中这里的白名单、且 execution_type == "builtin" 的工具才会真正执行；
其余情况一律显式失败，绝不做「悄悄降级」。

V0.2 不提供任何代码执行能力：execution_type == "executor" 直接拒绝，
真正的隔离要等 V0.3 的容器 / gVisor / Firecracker（见 docs/security.md）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from app.core.errors import AppError, ErrorCode
from app.models.entities import ToolDefinition, now_utc

logger = logging.getLogger(__name__)

BuiltinImplementation = Callable[[dict[str, Any]], dict[str, Any]]


def _echo(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"text": arguments.get("text", "")}


def _current_time(arguments: dict[str, Any]) -> dict[str, Any]:
    now = now_utc()
    return {"utc": now.isoformat(), "epoch_ms": int(now.timestamp() * 1000)}


# 内建实现的唯一白名单。名字不在这里，就无法执行。
BUILTIN_IMPLEMENTATIONS: dict[str, BuiltinImplementation] = {
    "echo": _echo,
    "current_time": _current_time,
}


async def execute_tool(tool: ToolDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool.execution_type != "builtin":
        raise AppError(
            ErrorCode.EXECUTOR_NOT_AVAILABLE,
            f"execution_type={tool.execution_type} 需要 V0.3 隔离执行器，当前版本不可用",
            status_code=501,
        )

    implementation = BUILTIN_IMPLEMENTATIONS.get(tool.name)
    if implementation is None:
        raise AppError(
            ErrorCode.TOOL_EXECUTION_FAILED,
            f"工具 {tool.name} 没有可用的内置实现",
            status_code=500,
        )

    try:
        # 内建实现是同步的，放线程池跑以适配超时控制。
        return await asyncio.wait_for(
            asyncio.to_thread(implementation, arguments),
            timeout=tool.timeout_seconds,
        )
    except TimeoutError as exc:
        raise AppError(
            ErrorCode.TOOL_TIMEOUT,
            f"工具执行超时（{tool.timeout_seconds}s）",
            status_code=504,
        ) from exc
    except AppError:
        raise
    except Exception as exc:  # 防御性兜底：实现内部的异常不向外泄露细节
        logger.exception("tool execution crashed", extra={"fields": {"tool": tool.name}})
        raise AppError(ErrorCode.TOOL_EXECUTION_FAILED, "工具执行失败", status_code=500) from exc

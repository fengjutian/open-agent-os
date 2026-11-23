"""工具执行。

关键安全性质：**注册一个 ToolDefinition 不会产生任何执行能力**。
只有下面三条路径之一能真正执行，其余一律显式失败，绝不「悄悄降级」：

1. `builtin` —— 名字命中内建白名单（echo、current_time）；
2. `mcp` —— 转发给 `settings.mcp_servers` 里**由运维在环境变量中声明**的服务器，
   数据库里只保存服务器名，不能携带命令行；
3. `executor` —— V0.3 的隔离执行器，当前一律拒绝。

MCP 是「借壳执行」的高风险点：拥有 write:tools 的管理员只能引用已声明的服务器，
无法通过注册工具来跑任意二进制。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.telemetry import span
from app.models.entities import ToolDefinition, now_utc
from app.services.mcp_client import McpError, McpStdioClient

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
    if tool.execution_type == "builtin":
        return await _execute_builtin(tool, arguments)
    if tool.execution_type == "mcp":
        return await _execute_mcp(tool, arguments)

    raise AppError(
        ErrorCode.EXECUTOR_NOT_AVAILABLE,
        f"execution_type={tool.execution_type} 需要 V0.3 隔离执行器，当前版本不可用",
        status_code=501,
    )


async def _execute_builtin(tool: ToolDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
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


async def _execute_mcp(tool: ToolDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
    config = tool.config or {}
    server_name = str(config.get("server") or "")
    remote_tool = str(config.get("tool") or tool.name)

    if not server_name:
        raise AppError(
            ErrorCode.TOOL_EXECUTION_FAILED,
            f"工具 {tool.name} 是 MCP 类型，但缺少 config.server",
            status_code=500,
        )

    server = settings.mcp_servers.get(server_name)
    if server is None:
        raise AppError(
            ErrorCode.TOOL_EXECUTION_FAILED,
            f"MCP 服务器 {server_name} 未在配置中声明（只能由运维通过 MCP_SERVERS 声明）",
            status_code=500,
        )

    timeout = min(tool.timeout_seconds or settings.mcp_timeout_seconds, settings.mcp_timeout_seconds)
    client = McpStdioClient(
        command=server.command,
        args=server.args,
        env=server.env,
        # 客户端读超时作为兜底，故意比整体预算宽松：让外层 wait_for 先触发，
        # 这样超时语义稳定映射成 TOOL_TIMEOUT，而不是随机的 MCP 读超时。
        timeout=timeout + 5.0,
    )

    with span("mcp.call", **{"mcp.server": server_name, "mcp.tool": remote_tool}) as current:
        try:
            await asyncio.wait_for(client.start(), timeout=timeout)
            result = await asyncio.wait_for(client.call_tool(remote_tool, arguments), timeout=timeout)
        except TimeoutError as exc:
            raise AppError(
                ErrorCode.TOOL_TIMEOUT,
                f"MCP 调用超时（{timeout}s）",
                status_code=504,
            ) from exc
        except McpError as exc:
            logger.warning(
                "mcp call failed",
                extra={"fields": {"tool": tool.name, "server": server_name, "error": exc.message}},
            )
            raise AppError(
                ErrorCode.MCP_ERROR,
                f"MCP 调用失败：{exc.message}",
                status_code=502,
            ) from exc
        finally:
            # 被取消时也必须把子进程收掉，否则会泄漏。
            with contextlib.suppress(Exception):
                await asyncio.shield(client.close())

        current.set_attribute("mcp.is_error", result.is_error)
        if result.is_error:
            detail = result.text()[:200]
            raise AppError(
                ErrorCode.TOOL_EXECUTION_FAILED,
                f"MCP 工具返回错误：{detail}",
                status_code=502,
            )

        return {"content": result.content, "text": result.text()}

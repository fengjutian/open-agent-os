"""工具执行。

关键安全性质：**注册一个 ToolDefinition 不会产生任何执行能力**。
只有下面三条路径之一能真正执行，其余一律显式失败，绝不「悄悄降级」：

1. `builtin` —— 名字命中内建白名单（echo、current_time）；
2. `mcp` —— 转发给 `settings.mcp_servers` 里**由运维在环境变量中声明**的服务器，
   数据库里只保存服务器名，不能携带命令行；
3. `executor` —— 交给 Rust Executor，在 Docker 容器里执行（V0.3）。

MCP 是「借壳执行」的高风险点：拥有 write:tools 的管理员只能引用已声明的服务器，
无法通过注册工具来跑任意二进制。executor 同理：镜像与运行时由工具定义约束，
命令只能来自模型参数，并且完整经过 schema 校验、策略与审计。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.telemetry import span
from app.models.entities import ToolDefinition, now_utc
from app.services.artifacts import (
    ArtifactStore,
    collect_from_workspace,
    resolve_input_mounts,
)
from app.services.executor_client import ExecutorClient, ExecutorRun
from app.services.mcp_client import McpError
from app.services.mcp_pool import get_mcp_pool

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


async def execute_tool(
    tool: ToolDefinition,
    arguments: dict[str, Any],
    *,
    invocation_id: str | None = None,
    run_id: str | None = None,
    artifact_store: ArtifactStore | None = None,
) -> dict[str, Any]:
    if tool.execution_type == "builtin":
        return await _execute_builtin(tool, arguments)
    if tool.execution_type == "mcp":
        return await _execute_mcp(tool, arguments)
    if tool.execution_type == "executor":
        return await _execute_executor(
            tool,
            arguments,
            invocation_id=invocation_id,
            run_id=run_id,
            artifact_store=artifact_store,
        )

    raise AppError(
        ErrorCode.EXECUTOR_NOT_AVAILABLE,
        f"execution_type={tool.execution_type} 没有可用的执行后端",
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
    pool = get_mcp_pool()

    with span("mcp.call", **{"mcp.server": server_name, "mcp.tool": remote_tool}) as current:
        try:
            async with pool.session(server_name, server, timeout=timeout) as client:
                result = await asyncio.wait_for(
                    client.call_tool(remote_tool, arguments), timeout=timeout
                )
        except TimeoutError as exc:
            # 响应可能还在路上，连接不能复用
            await pool.reset(server_name)
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

        current.set_attribute("mcp.is_error", result.is_error)
        if result.is_error:
            detail = result.text()[:200]
            raise AppError(
                ErrorCode.TOOL_EXECUTION_FAILED,
                f"MCP 工具返回错误：{detail}",
                status_code=502,
            )

        return {"content": result.content, "text": result.text()}


async def _execute_executor(
    tool: ToolDefinition,
    arguments: dict[str, Any],
    *,
    invocation_id: str | None,
    run_id: str | None,
    artifact_store: ArtifactStore | None,
) -> dict[str, Any]:
    """在 Docker 容器里执行命令，并把产物登记下来。

    命令只能来自调用参数（`config.command_argument`，默认 `command`），
    工具定义里不允许写死命令——否则「注册工具」就又变成了一条任意命令通道。
    """
    config = tool.config or {}
    runtime = str(config.get("runtime") or "")
    argument_name = str(config.get("command_argument") or "command")
    command = arguments.get(argument_name)
    if not isinstance(command, str) or not command.strip():
        raise AppError(
            ErrorCode.TOOL_ARGUMENTS_INVALID,
            f"隔离执行器工具必须提供非空字符串参数 {argument_name}",
            status_code=422,
        )

    image = str(config.get("image") or settings.executor_image)
    timeout = min(
        float(tool.timeout_seconds or settings.executor_timeout_seconds),
        settings.executor_timeout_seconds,
    )

    # 输入文件：只能挂本租户登记过的产物，且一律只读。
    inputs: list[dict[str, str]] = []
    if artifact_store is not None:
        inputs = resolve_input_mounts(artifact_store, config, arguments)

    with span(
        "executor.run",
        **{"executor.runtime": runtime, "executor.image": image},
    ) as current:
        run = await ExecutorClient().run(
            tool_name=tool.name,
            runtime=runtime,
            command=command,
            image=image,
            timeout_seconds=timeout,
            inputs=inputs,
        )
        current.set_attribute("executor.status", run.status)
        if run.exit_code is not None:
            current.set_attribute("executor.exit_code", run.exit_code)
        current.set_attribute("executor.artifacts", len(run.artifacts))

    logger.info(
        "executor run finished",
        extra={
            "fields": {
                "tool": tool.name,
                "status": run.status,
                "exit_code": run.exit_code,
                "duration_ms": run.duration_ms,
                "artifacts": len(run.artifacts),
            }
        },
    )

    try:
        if run.status == "timeout":
            raise AppError(
                ErrorCode.TOOL_TIMEOUT,
                f"容器执行超时（{timeout:g}s）",
                status_code=504,
            )
        if run.status != "succeeded":
            raise AppError(
                ErrorCode.TOOL_EXECUTION_FAILED,
                _failure_message(run),
                status_code=502,
            )

        collected = collect_from_workspace(run.workspace_dir, run.artifacts)
        rows = []
        if artifact_store is not None and invocation_id and run_id:
            try:
                rows = artifact_store.persist(
                    invocation_id=invocation_id,
                    run_id=run_id,
                    tool_name=tool.name,
                    artifacts=collected,
                )
            except OSError as exc:
                logger.exception(
                    "artifact persist failed",
                    extra={"fields": {"tool": tool.name, "invocation_id": invocation_id}},
                )
                raise AppError(
                    ErrorCode.TOOL_EXECUTION_FAILED,
                    "命令已执行，但产物写入失败",
                    status_code=500,
                ) from exc

        stored = {row.path: row for row in rows}
        artifacts_payload = []
        for item in collected:
            entry: dict[str, Any] = {
                "path": item.relative_path,
                "size": item.size,
                "sha256": item.sha256,
            }
            row = stored.get(item.relative_path)
            if row is not None:
                entry["artifact_id"] = row.id
            artifacts_payload.append(entry)

        return {
            "exit_code": run.exit_code,
            "stdout": run.stdout,
            "stderr": run.stderr,
            "stdout_truncated": run.stdout_truncated,
            "stderr_truncated": run.stderr_truncated,
            "duration_ms": run.duration_ms,
            "artifacts": artifacts_payload,
        }
    finally:
        # 工作目录里有本次调用的临时文件，无论成败都清掉。
        shutil.rmtree(run.workspace_dir, ignore_errors=True)


def _failure_message(run: ExecutorRun) -> str:
    detail = run.stderr.strip()
    if len(detail) > 800:
        detail = "…" + detail[-800:]
    prefix = (
        f"命令执行失败（exit code {run.exit_code}）"
        if run.exit_code is not None
        else "命令执行失败"
    )
    return f"{prefix}：{detail}" if detail else prefix

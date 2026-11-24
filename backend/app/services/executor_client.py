"""Python 侧的隔离执行器客户端。

一次调用 = 启动一个 `agent-executor` 子进程，stdin 写 ExecutionRequest，
stdout 读 ExecutionResult。真正的隔离在 Rust 进程里由 Docker 提供，
这里只负责：

- 生成每次调用独立的工作目录（`workspace/output` 是产物目录）；
- 把工具参数翻译成容器内的 argv（shell / python 两种 runtime）；
- 在外层再加一道超时，防止执行器自身卡死；
- 把执行器故障映射成稳定的错误码，不把原始堆栈丢给调用方。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "1"
# 与 Rust 侧 MAX_REQUEST_BYTES 保持一致。
MAX_REQUEST_BYTES = 1024 * 1024
_VALID_STATUSES = {"succeeded", "failed", "timeout"}


@dataclass(frozen=True)
class ExecutorArtifact:
    """执行器报告的产物条目；大小由宿主机侧再次校验。"""

    path: str
    size: int


@dataclass(frozen=True)
class ExecutorRun:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    artifacts: list[ExecutorArtifact]
    workspace_dir: Path


# 每个事件循环一个信号量。测试里每个用例用 asyncio.run 新建循环，
# 复用同一个 Semaphore 会在跨循环时出问题，所以按 loop id 分开存。
_semaphores: dict[int, asyncio.Semaphore] = {}


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(id(loop))
    if semaphore is None:
        semaphore = asyncio.Semaphore(max(1, settings.executor_max_concurrency))
        _semaphores[id(loop)] = semaphore
    return semaphore


def _executor_command() -> list[str]:
    raw = settings.executor_command.strip()
    if not raw:
        raise AppError(
            ErrorCode.EXECUTOR_NOT_AVAILABLE,
            "EXECUTOR_COMMAND 为空，无法启动隔离执行器",
            status_code=501,
        )
    # Windows 路径里的反斜杠会被 shlex 当转义符；如果整串就是一个存在的文件，
    # 直接当成单一路径，避免用户被迫写正斜杠。
    if Path(raw).is_file():
        return [raw]
    parts = shlex.split(raw, posix=True)
    if not parts:
        raise AppError(
            ErrorCode.EXECUTOR_NOT_AVAILABLE,
            "EXECUTOR_COMMAND 解析后为空，无法启动隔离执行器",
            status_code=501,
        )
    return parts


def _default_user() -> str:
    """容器内默认用户。

    POSIX 上用后端进程自己的 uid:gid：这样 workspace（mkdtemp 的 0700）不需要放开权限，
    产物也归后端用户所有。Windows 的 Docker Desktop 绑定挂载不按 uid 鉴权，用 0:0。
    """
    if os.name == "posix":
        return f"{os.getuid()}:{os.getgid()}"
    return "0:0"


def command_argv(runtime: str, command: str) -> list[str]:
    """把 runtime + 命令翻译成容器内 argv。

    注意这里不做任何字符串拼接式的 shell 包装：`sh -lc` 是容器内解释执行，
    宿主机只负责把参数原样传给 docker，因此不存在宿主机命令注入。
    """
    if runtime == "shell":
        return ["sh", "-lc", command]
    if runtime == "python":
        return ["python", "-I", "-B", "-c", command]
    raise AppError(
        ErrorCode.TOOL_EXECUTION_FAILED,
        f"不支持的 executor runtime: {runtime}",
        status_code=500,
    )


class ExecutorClient:
    async def run(
        self,
        *,
        tool_name: str,
        runtime: str,
        command: str,
        image: str,
        timeout_seconds: float,
        inputs: list[dict[str, str]] | None = None,
    ) -> ExecutorRun:
        if not settings.executor_enabled:
            raise AppError(
                ErrorCode.EXECUTOR_NOT_AVAILABLE,
                "隔离执行器未启用（需要 EXECUTOR_ENABLED=true、Docker 与 agent-executor）",
                status_code=501,
            )

        workspace = Path(
            tempfile.mkdtemp(prefix="agent-exec-", dir=settings.executor_workdir or None)
        )
        try:
            (workspace / "output").mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": PROTOCOL_VERSION,
                "tool": tool_name,
                "image": image,
                "argv": command_argv(runtime, command),
                "workspace_dir": str(workspace),
                "timeout_seconds": timeout_seconds,
                "memory_mb": settings.executor_memory_mb,
                "cpus": settings.executor_cpus,
                "pids_limit": settings.executor_pids_limit,
                "max_output_bytes": settings.executor_max_output_bytes,
                "max_artifact_bytes": settings.executor_max_artifact_bytes,
                "max_artifacts": settings.executor_max_artifacts,
                # 只读输入挂载：来源与目标都已由 Python 侧校验过
                "inputs": inputs or [],
            }
            # 显式指定容器用户：与 workspace 的属主保持一致，避免放宽目录权限。
            payload["user"] = settings.executor_user or _default_user()

            body = await self._invoke(payload, timeout_seconds)
            return self._to_run(body, workspace)
        except BaseException:
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    async def _invoke(self, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_REQUEST_BYTES:
            raise AppError(
                ErrorCode.TOOL_ARGUMENTS_INVALID,
                f"执行请求超过 {MAX_REQUEST_BYTES} 字节上限",
                status_code=422,
            )

        outer_timeout = max(0.5, timeout_seconds + settings.executor_grace_seconds)
        command = _executor_command()
        async with _semaphore():
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise AppError(
                    ErrorCode.EXECUTOR_NOT_AVAILABLE,
                    f"找不到执行器可执行文件：{command[0]}（请构建 rust-executor 或配置 EXECUTOR_COMMAND）",
                    status_code=501,
                ) from exc
            except OSError as exc:
                logger.warning(
                    "failed to spawn executor",
                    extra={"fields": {"command": command[0], "error": str(exc)}},
                )
                raise AppError(
                    ErrorCode.EXECUTOR_ERROR, "无法启动隔离执行器", status_code=502
                ) from exc

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(raw), timeout=outer_timeout
                )
            except TimeoutError as exc:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=5)
                raise AppError(
                    ErrorCode.TOOL_TIMEOUT,
                    f"执行器在 {outer_timeout:.0f}s 内没有返回结果",
                    status_code=504,
                ) from exc
            except BaseException:
                # 取消（例如服务关闭）时也要收掉子进程，避免留下孤儿执行器。
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=5)
                raise

        if process.returncode not in (0, 1):
            logger.warning(
                "executor exited unexpectedly",
                extra={
                    "fields": {
                        "returncode": process.returncode,
                        "stderr": stderr.decode("utf-8", "replace")[:500],
                    }
                },
            )

        try:
            body = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning(
                "executor returned non-JSON output",
                extra={"fields": {"stderr": stderr.decode("utf-8", "replace")[:500]}},
            )
            raise AppError(
                ErrorCode.EXECUTOR_ERROR, "隔离执行器返回了无法解析的结果", status_code=502
            ) from None

        if not isinstance(body, dict) or body.get("schema_version") != PROTOCOL_VERSION:
            raise AppError(
                ErrorCode.EXECUTOR_ERROR, "隔离执行器返回了不兼容的协议版本", status_code=502
            )
        return body

    def _to_run(self, body: dict[str, Any], workspace: Path) -> ExecutorRun:
        status = str(body.get("status") or "")
        if status == "rejected":
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            code = str(error.get("code") or "")
            message = str(error.get("message") or "").strip()[:300]
            if code == "executor_unavailable":
                raise AppError(
                    ErrorCode.EXECUTOR_NOT_AVAILABLE,
                    message or "隔离执行器不可用（Docker 未就绪）",
                    status_code=501,
                )
            raise AppError(
                ErrorCode.EXECUTOR_ERROR,
                message or "隔离执行器拒绝了执行请求",
                status_code=502,
            )

        if status not in _VALID_STATUSES:
            raise AppError(
                ErrorCode.EXECUTOR_ERROR,
                f"隔离执行器返回了未知状态：{status or '<empty>'}",
                status_code=502,
            )

        artifacts: list[ExecutorArtifact] = []
        for item in body.get("artifacts") or []:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            size = item.get("size")
            if isinstance(path, str) and isinstance(size, int) and size >= 0:
                artifacts.append(ExecutorArtifact(path=path, size=size))

        return ExecutorRun(
            status=status,
            exit_code=body.get("exit_code") if isinstance(body.get("exit_code"), int) else None,
            stdout=str(body.get("stdout") or ""),
            stderr=str(body.get("stderr") or ""),
            stdout_truncated=bool(body.get("stdout_truncated")),
            stderr_truncated=bool(body.get("stderr_truncated")),
            duration_ms=body.get("duration_ms")
            if isinstance(body.get("duration_ms"), int)
            else 0,
            artifacts=artifacts,
            workspace_dir=workspace,
        )

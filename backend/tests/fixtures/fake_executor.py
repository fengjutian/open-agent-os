"""测试用的假执行器。

不启动 Docker，也不执行任何真实命令；只按 `FAKE_EXECUTOR_MODE` 返回预先编排的
ExecutionResult，用来在 CI 里验证 Python 侧的协议、错误映射与产物落盘。

它刻意保持「读 stdin、写 stdout、退出」的形态，和真正的 `agent-executor` 一致。
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path


def emit(payload: dict, code: int = 0) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()
    raise SystemExit(code)


def result(
    status: str = "succeeded",
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    artifacts: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": "1",
        "status": status,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "duration_ms": 3,
        "artifacts": artifacts or [],
    }


def main() -> None:
    try:
        request = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        emit(
            {
                "schema_version": "1",
                "status": "rejected",
                "error": {"code": "invalid_request", "message": "bad json"},
            },
            1,
        )
        return

    workspace = Path(request["workspace_dir"])
    output = workspace / "output"
    output.mkdir(parents=True, exist_ok=True)

    mode = os.environ.get("FAKE_EXECUTOR_MODE", "success")
    argv = request.get("argv") or []
    command = argv[-1] if argv else ""

    if mode == "success":
        content = b"hello artifact"
        (output / "report.txt").write_bytes(content)
        manifest = [
            {
                "path": "report.txt",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ]
        emit(result(stdout=f"hello\nargv={command}\n", artifacts=manifest))
    elif mode == "nested":
        nested = output / "logs"
        nested.mkdir(exist_ok=True)
        content = b"nested"
        (nested / "run.log").write_bytes(content)
        emit(
            result(
                artifacts=[{"path": "logs/run.log", "size": len(content)}],
            )
        )
    elif mode == "fail":
        emit(result(status="failed", exit_code=2, stderr="boom: something broke"))
    elif mode == "timeout":
        time.sleep(10)
        emit(result(stdout="too late\n"))
    elif mode == "reject":
        emit(
            {
                "schema_version": "1",
                "status": "rejected",
                "error": {
                    "code": "executor_unavailable",
                    "message": "Cannot connect to the Docker daemon",
                },
            }
        )
    elif mode == "garbage":
        sys.stdout.write("this is not json")
        sys.stdout.flush()
        raise SystemExit(0)
    elif mode == "traversal":
        # 同时在 output 之外写一个文件：即使它被写出来，也不应该被收集。
        (workspace / "evil.txt").write_bytes(b"evil")
        emit(result(artifacts=[{"path": "../evil.txt", "size": 4}]))
    elif mode == "size_mismatch":
        content = b"actual"
        (output / "report.txt").write_bytes(content)
        emit(result(artifacts=[{"path": "report.txt", "size": 999}]))
    elif mode == "duplicate":
        content = b"dup"
        (output / "report.txt").write_bytes(content)
        entry = {"path": "report.txt", "size": len(content)}
        emit(result(artifacts=[entry, dict(entry)]))
    else:
        emit(
            {
                "schema_version": "1",
                "status": "rejected",
                "error": {"code": "unknown_mode", "message": f"unknown mode {mode}"},
            }
        )


if __name__ == "__main__":
    main()

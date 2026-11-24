#!/usr/bin/env python3
"""统一本地开发启动脚本。

一条命令完成：准备依赖 -> 启动后端（uvicorn --reload）-> 启动前端（Vite）
-> 等两边就绪后打印地址 -> Ctrl+C 一起停止。

用法::

    python scripts/dev.py                    # 8000 + 5173
    python scripts/dev.py --executor         # 额外构建并启用 Docker 执行器
    python scripts/dev.py --backend-only     # 只起后端
    python scripts/dev.py --frontend-only    # 只起前端
    python scripts/dev.py --python C:/Python312/python.exe --no-setup
    python scripts/dev.py --install          # 强制重装依赖

Windows 也可以用 `scripts\\dev.ps1`，macOS/Linux 用 `scripts/dev.sh`。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
RUST_EXECUTOR = ROOT / "rust-executor"
IS_WINDOWS = os.name == "nt"

# 后端首次启动会把 admin key 打进日志；抓出来单独提示一次。
_KEY_RE = re.compile(r'"api_key"\s*:\s*"(oak_[A-Za-z0-9_\-]+)"')
_key_shown = False


def say(message: str = "") -> None:
    print(message, flush=True)


def configure_output() -> None:
    """让转发日志里的任意字符都不会把打印线程搞崩。

    - 输出到管道/文件时统一用 UTF-8（CI、重定向、日志采集都按 UTF-8 读）；
    - 输出到控制台时保留系统编码，但把不可编码字符替换掉（例如 Vite 的 "➜"）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except Exception:
            pass


def step(message: str) -> None:
    say(f"\n==> {message}")


def fail(message: str) -> None:
    print(f"\n错误：{message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    say("    $ " + " ".join(str(part) for part in cmd))
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def which(name: str) -> str:
    path = shutil.which(name)
    if not path:
        fail(f"找不到 {name}，请先安装并加入 PATH")
    return path


# ---------------------------------------------------------------------------
# 依赖准备
# ---------------------------------------------------------------------------


def venv_python() -> Path:
    return BACKEND / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def python_deps_ready(interpreter: str | Path) -> bool:
    probe = subprocess.run(
        [str(interpreter), "-c", "import fastapi, uvicorn, sqlalchemy, alembic, jsonschema"],
        cwd=BACKEND,
        capture_output=True,
    )
    return probe.returncode == 0


def prepare_backend(args: argparse.Namespace) -> str:
    if args.python:
        interpreter = Path(args.python)
        if not interpreter.exists():
            fail(f"--python 指定的解释器不存在：{interpreter}")
        if not python_deps_ready(interpreter):
            if args.no_setup:
                fail(
                    f"{interpreter} 缺少后端依赖，去掉 --no-setup "
                    "或先 pip install -r requirements.txt"
                )
            step(f"用 {interpreter} 安装后端依赖")
            run([str(interpreter), "-m", "pip", "install", "-r", "requirements.txt"], cwd=BACKEND)
        return str(interpreter)

    interpreter = venv_python()
    ready = interpreter.exists() and python_deps_ready(interpreter)
    if ready and not args.install:
        return str(interpreter)

    if args.no_setup:
        # 允许退回当前解释器（例如 CI 或已经全局装好依赖的环境）。
        if python_deps_ready(sys.executable):
            say("    未使用 venv，沿用当前 Python 解释器（--no-setup）")
            return sys.executable
        fail("后端依赖未安装；去掉 --no-setup，或先用 --python 指定已装好依赖的解释器")

    if not interpreter.exists() or args.install:
        step("创建 backend/.venv")
        run([sys.executable, "-m", "venv", str(BACKEND / ".venv")], cwd=BACKEND)

    step("安装后端依赖（requirements.txt）")
    run([str(interpreter), "-m", "pip", "install", "-r", "requirements.txt"], cwd=BACKEND)
    return str(interpreter)


def prepare_frontend(args: argparse.Namespace) -> str:
    npm = which("npm")
    vite = FRONTEND / "node_modules" / ".bin" / ("vite.CMD" if IS_WINDOWS else "vite")
    if vite.exists() and not args.install:
        return npm

    if args.no_setup:
        fail("前端依赖未安装；去掉 --no-setup，或先在 frontend 下执行 npm ci")

    step("安装前端依赖（npm ci）")
    run([npm, "ci"], cwd=FRONTEND)
    return npm


def prepare_executor(env: dict[str, str]) -> None:
    cargo = which("cargo")
    step("构建 Rust Executor（release）")
    run(
        [cargo, "build", "--release", "--manifest-path", str(RUST_EXECUTOR / "Cargo.toml")],
        cwd=ROOT,
    )
    binary = RUST_EXECUTOR / "target" / "release" / (
        "agent-executor.exe" if IS_WINDOWS else "agent-executor"
    )
    if not binary.exists():
        fail(f"构建完成但找不到执行器：{binary}")

    env["EXECUTOR_ENABLED"] = "true"
    env["EXECUTOR_COMMAND"] = str(binary)
    say(f"    执行器：{binary}")

    probe = subprocess.run([str(binary), "--check"], capture_output=True, text=True)
    detail = (probe.stdout or probe.stderr).strip()
    if detail:
        say(f"    --check：{detail}")
    if probe.returncode != 0:
        say("    警告：Docker 未就绪；executor 工具会返回 executor_not_available")


# ---------------------------------------------------------------------------
# 进程管理
# ---------------------------------------------------------------------------


def pump(process: subprocess.Popen, prefix: str, on_line=None) -> None:
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip("\r\n")
        try:
            say(f"{prefix} {line}")
        except Exception:
            # 日志转发失败不应该结束读取线程（否则子进程管道会被写满）
            say(f"{prefix} [unprintable output]")
        if on_line is not None:
            # 提示逻辑不能影响日志转发
            with contextlib.suppress(Exception):
                on_line(line)


def start(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    prefix: str,
    on_line=None,
) -> subprocess.Popen:
    kwargs: dict = {
        "cwd": cwd,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(cmd, **kwargs)
    threading.Thread(target=pump, args=(process, prefix, on_line), daemon=True).start()
    return process


def terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            # /T 连 uvicorn --reload 的子进程、npm 拉起的 node 一起收掉
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=20,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except Exception:
        with contextlib.suppress(Exception):
            process.kill()
    with contextlib.suppress(Exception):
        process.wait(timeout=10)


def ensure_port_free(port: int, label: str) -> None:
    """启动前先占一次端口：避免健康检查误命中旧进程，问题也能早暴露。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            fail(
                f"{label}端口 {port} 不可用（{exc}）；"
                f"请换端口：--backend-port / --frontend-port"
            )


def wait_http(url: str, process: subprocess.Popen, timeout: float, label: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            fail(f"{label}启动失败（退出码 {process.returncode}），请检查上面的日志")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    return
        except Exception:
            pass
        time.sleep(0.3)
    fail(f"等待{label}就绪超时（{timeout:.0f}s）：{url}")


def watch_admin_key(line: str) -> None:
    global _key_shown
    if _key_shown:
        return
    match = _KEY_RE.search(line)
    if not match:
        return
    _key_shown = True
    key = match.group(1)
    say("")
    say("=" * 68)
    say("  首次启动的 admin API Key（只显示这一次，请立即保存）")
    say(f"  {key}")
    say("  粘贴到前端登录框；或写入 backend/.env 的 BOOTSTRAP_ADMIN_API_KEY")
    say("=" * 68)
    say("")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统一启动后端 + 前端（并可选启用 Docker 执行器）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--executor", action="store_true", help="构建并启用隔离执行器")
    parser.add_argument("--backend-only", action="store_true")
    parser.add_argument("--frontend-only", action="store_true")
    parser.add_argument("--no-setup", action="store_true", help="不自动创建 venv / 安装依赖")
    parser.add_argument("--install", action="store_true", help="强制重装依赖")
    parser.add_argument("--python", default="", help="使用指定 Python 解释器（跳过 venv）")
    parser.add_argument("--timeout", type=float, default=120.0, help="等待就绪的秒数")
    args = parser.parse_args()
    if args.backend_only and args.frontend_only:
        parser.error("--backend-only 与 --frontend-only 不能同时使用")
    return args


def main() -> int:
    configure_output()
    # pip / venv 等子进程也按 UTF-8 跑：requirements 里有中文注释，
    # 只靠 coding 头还不够时这层是兜底。
    os.environ.setdefault("PYTHONUTF8", "1")
    if sys.version_info < (3, 12):  # noqa: UP036 - 给旧解释器一个可读的错误
        fail(f"需要 Python 3.12+（当前 {sys.version.split()[0]}）")
    args = parse_args()
    if not BACKEND.is_dir() or not FRONTEND.is_dir():
        fail(f"请在仓库根目录运行（找不到 {BACKEND} 或 {FRONTEND}）")

    if not args.frontend_only:
        ensure_port_free(args.backend_port, "后端")
    if not args.backend_only:
        ensure_port_free(args.frontend_port, "前端")

    say("Enterprise Agent Platform - 统一启动")
    say(f"仓库：{ROOT}")

    backend_env = os.environ.copy()
    # 让后端日志按 UTF-8 输出，Windows 控制台也不会因编码把 JSON 拆坏。
    backend_env["PYTHONUTF8"] = "1"
    backend_env["PYTHONIOENCODING"] = "utf-8"
    backend_env["CORS_ORIGINS"] = (
        f"http://localhost:{args.frontend_port},http://127.0.0.1:{args.frontend_port}"
    )

    if args.executor:
        prepare_executor(backend_env)

    children: list[tuple[str, subprocess.Popen]] = []
    backend_proc: subprocess.Popen | None = None
    frontend_proc: subprocess.Popen | None = None

    try:
        if not args.frontend_only:
            interpreter = prepare_backend(args)
            step(f"启动后端（http://localhost:{args.backend_port}）")
            backend_proc = start(
                [
                    interpreter,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.backend_port),
                    "--reload",
                ],
                BACKEND,
                backend_env,
                "[backend]",
                watch_admin_key,
            )
            children.append(("后端", backend_proc))

        if not args.backend_only:
            npm = prepare_frontend(args)
            frontend_env = os.environ.copy()
            frontend_env["VITE_API_BASE_URL"] = (
                f"http://localhost:{args.backend_port}/api/v1"
            )
            step(f"启动前端（http://localhost:{args.frontend_port}）")
            frontend_proc = start(
                [
                    npm,
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.frontend_port),
                    "--strictPort",
                ],
                FRONTEND,
                frontend_env,
                "[frontend]",
            )
            children.append(("前端", frontend_proc))

        if backend_proc is not None:
            wait_http(
                f"http://127.0.0.1:{args.backend_port}/health",
                backend_proc,
                args.timeout,
                "后端",
            )
        if frontend_proc is not None:
            wait_http(
                f"http://127.0.0.1:{args.frontend_port}/",
                frontend_proc,
                args.timeout,
                "前端",
            )

        say("")
        say("=" * 68)
        if backend_proc is not None:
            say(f"  后端    http://localhost:{args.backend_port}")
            say(f"  API 文档 http://localhost:{args.backend_port}/docs")
        if frontend_proc is not None:
            say(f"  前端    http://localhost:{args.frontend_port}")
        if args.executor:
            say("  执行器  已启用（需要 Docker daemon 正在运行）")
        say("  按 Ctrl+C 停止全部进程")
        say("=" * 68)

        while True:
            time.sleep(0.4)
            for name, process in children:
                code = process.poll()
                if code is not None:
                    say(f"\n{name}已退出（exit {code}），正在停止其余进程。")
                    return 1
    except KeyboardInterrupt:
        say("\n收到 Ctrl+C，正在停止…")
        return 0
    finally:
        for _, process in children:
            terminate(process)
        say("已停止。")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        command = " ".join(str(part) for part in exc.cmd)
        print(
            f"\n错误：命令失败（exit {exc.returncode}）：{command}\n"
            "  常见原因：依赖装不上（网络/镜像源）、端口被占用、或缺少 Node/Rust 工具链。",
            file=sys.stderr,
        )
        sys.exit(exc.returncode or 1)

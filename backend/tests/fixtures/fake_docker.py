"""测试用的假 docker CLI。

不连接任何 Docker daemon：按 `FAKE_DOCKER_MODE` 模拟成功 / 容器失败 / daemon 不可用 /
挂起。用来在 CI 里把真实的 `agent-executor` 二进制跑通（协议、超时、杀容器、产物扫描），
而不需要 Linux 容器运行时。

用法：设置 `AGENT_EXECUTOR_DOCKER_BIN='"<python>" "<本文件>"'`。
"""

import os
import sys
import time
from pathlib import Path


def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit(0)

    # Rust 超时后会调用 `docker kill <name>` / `docker rm -f <name>`，直接成功即可。
    if args[0] in {"kill", "rm"}:
        sys.exit(0)

    mode = os.environ.get("FAKE_DOCKER_MODE", "success")
    if mode == "daemon_down":
        sys.stderr.write("Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n")
        sys.exit(125)
    if mode == "fail":
        sys.stderr.write("container boom\n")
        sys.exit(3)
    if mode == "timeout":
        time.sleep(30)
        sys.exit(0)

    # 从 `-v <host>:/workspace:rw` 里取出宿主机路径，模拟容器往 /workspace 写文件。
    mount = ""
    for index, arg in enumerate(args):
        if arg == "-v" and index + 1 < len(args):
            mount = args[index + 1]
            break
    if mount.endswith(":/workspace:rw"):
        host = mount[: -len(":/workspace:rw")]
        output = Path(host) / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "report.txt").write_text("from fake docker", encoding="utf-8")

    sys.stdout.write("container says hi\n")
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    main()

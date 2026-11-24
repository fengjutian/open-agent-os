# Rust Executor — V0.3 Docker 后端

把一条 `ExecutionRequest` 放进 Docker 容器执行，返回结构化的 `ExecutionResult`。
它**不是** Sandbox 本身：隔离由 Docker（未来是 gVisor / Firecracker）提供，Rust 只负责
拼参数、看超时、收输出、扫产物。

## 用法

```bash
cargo build --release

# 健康检查：不执行任何东西，只探测 Docker 与默认值
./target/release/agent-executor --check

# 执行：stdin 一个 JSON，stdout 一个 JSON
echo '{
  "schema_version": "1",
  "tool": "run_python",
  "image": "python:3.12-slim",
  "argv": ["python", "-I", "-B", "-c", "print(1+1)"],
  "workspace_dir": "/tmp/agent-ws-1",
  "timeout_seconds": 10,
  "memory_mb": 256,
  "cpus": 1,
  "pids_limit": 64
}' | ./target/release/agent-executor
```

`workspace_dir` 必须是已存在的绝对路径，会被挂载到容器 `/workspace`；
命令写到 `/workspace/output/` 下的普通文件会出现在结果的 `artifacts` 里
（只报告 `path` 与 `size`，SHA-256 由 Python 侧重算）。

## 协议（schema_version = "1"）

请求：

```json
{
  "schema_version": "1",
  "tool": "run_python",
  "image": "python:3.12-slim",
  "argv": ["python", "-I", "-B", "-c", "..."],
  "workspace_dir": "/abs/path",
  "timeout_seconds": 10,
  "memory_mb": 512,
  "cpus": 1.0,
  "pids_limit": 128,
  "max_output_bytes": 65536,
  "max_artifact_bytes": 5242880,
  "max_artifacts": 20,
  "user": "65534:65534"
}
```

除 `schema_version` / `image` / `argv` / `workspace_dir` 外都可省略，省略时用默认值；
越界的值会被夹到允许区间。未知字段会被拒绝（严格模式）。

响应：

```json
{
  "schema_version": "1",
  "status": "succeeded",
  "exit_code": 0,
  "stdout": "2\n",
  "stderr": "",
  "stdout_truncated": false,
  "stderr_truncated": false,
  "duration_ms": 412,
  "artifacts": [{"path": "report.txt", "size": 14}]
}
```

| status | 含义 |
|---|---|
| `succeeded` | 容器退出码为 0 |
| `failed` | 容器正常退出但退出码非 0 |
| `timeout` | 超过 `timeout_seconds`，已杀容器 |
| `rejected` | 请求非法、Docker 不可用或 Docker 自身报错（`error` 字段带 code/message） |

退出码：`0` 表示产出了合法结果（包括工具本身失败/超时）；`1` 表示请求无法解析或校验失败。

`error.code` 取值：`invalid_request`、`unsupported_schema_version`、
`executor_unavailable`、`executor_error`。

## 隔离参数

```
docker run --rm --init --name agent-exec-<uuid>
  --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777
  --cap-drop ALL --security-opt no-new-privileges
  --pids-limit <n> --memory <n>m --memory-swap <n>m --cpus <n>
  --user <uid:gid> -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1
  -v <workspace>:/workspace:rw -w /workspace
  <image> <argv...>
```

- 只挂载本次 workspace，不挂宿主目录、不挂 docker socket；
- 超时会先 `docker kill <name>` 再 `docker rm -f`，避免容器残留；
- 镜像名必须是合法引用且不能以 `-` 开头，防止被 Docker 当成命令行选项；
- `AGENT_EXECUTOR_DOCKER_BIN` 可改成 `podman`、rootless docker 包装脚本等，
  支持带参数（按单/双引号拆分）。

## 容器用户

- Python 侧总是显式传 `user`：POSIX 用后端进程的 `uid:gid`（workspace 因此可以保持
  0700，不必对宿主放开权限），Windows 用 `0:0`；
- 直接调用本二进制且省略 `user` 时：Linux/macOS 默认 `65534:65534`（nobody），
  Windows 默认 `0:0`；
- 可以用请求里的 `user` 或 Python 侧的 `EXECUTOR_USER` 覆盖。

**不要**在普通 Rust 子进程里执行不可信代码并称之为 Sandbox。容器共享宿主机内核，
真正的强隔离需要 gVisor / Firecracker 一类的机制。

## 开发与测试

```bash
cargo test    # 协议、Docker 参数、产物扫描的单元测试，不需要 Docker
cargo build   # Python 侧的端到端测试会使用 target/debug/agent-executor
```

Python 侧用 `backend/tests/fixtures/fake_docker.py` 模拟 docker CLI，所以 CI 不需要
容器运行时也能验证「超时 → 杀容器 → 返回 timeout」这条链路（见
`backend/tests/test_executor.py`）。

## 后续

- Sandbox backend trait（Docker / gVisor / Firecracker 可切换）；
- 输入文件挂载、网络白名单、镜像预热与快照；
- 执行中流式 stdout/stderr（SSE 回传）；
- 容器级配额与并发调度（当前并发限制在 Python 侧）。

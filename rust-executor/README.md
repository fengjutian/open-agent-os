# Rust Executor — V0.3 placeholder

当前**不提供**任意代码执行能力，`cargo run` 只会解析请求并结构化拒绝。

## 为什么有 src/main.rs

此前本目录只有 Cargo.toml 和一个 README，没有任何 target，`cargo build` 会直接失败：

```text
error: failed to parse manifest: no targets specified in the manifest
```

现在补了一个最小二进制，做到两件事：

1. Cargo 包可编译、可 `cargo run`，不再是一个坏掉的占位；
2. 固定 ExecutionRequest / ExecutionResult 的版本化协议形状（`schema_version` = `1`）。

## 行为

从 stdin 读一个 JSON 请求，校验 `schema_version` 后**一律拒绝**，输出结构化结果并以退出码 1 结束：

```bash
echo '{"schema_version":"1","tool":"shell","input":{"cmd":"ls"}}' | cargo run --quiet
```

```json
{"schema_version":"1","status":"rejected","error":{"code":"executor_not_implemented","message":"..."}}
```

| code | 含义 |
|---|---|
| `invalid_request` | 空输入、非 JSON 或读取失败 |
| `unsupported_schema_version` | `schema_version` 不是 `1` |
| `executor_not_implemented` | 请求形状合法，但执行功能未实现（当前永远走这条） |

## 后续

实现 ExecutionRequest / ExecutionResult 版本化协议、Sandbox backend trait、Docker backend、超时取消、资源限制、stdout/stderr/exit code 收集、workspace 创建清理，再评估 gVisor / Firecracker。

安全提醒：不要在普通 Rust 子进程中执行不可信代码并称之为 Sandbox。必须使用 OS / 容器 / microVM 隔离机制。

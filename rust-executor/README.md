# Rust Executor — V0.3 placeholder

当前不提供任意代码执行能力。未来实现 ExecutionRequest / ExecutionResult 版本化协议、Sandbox backend trait、Docker backend、超时取消、资源限制、stdout/stderr/exit code 收集、workspace 创建清理，再评估 gVisor / Firecracker。

安全提醒：不要在普通 Rust 子进程中执行不可信代码并称之为 Sandbox。必须使用 OS / 容器 / microVM 隔离机制。

# 安全基线

## V0.1
- 模型 API Key 只从后端环境变量读取。
- CORS 限制来源。
- 不开放任意 Shell/Python/Node 执行接口。
- 不把模型输出视为可信指令。
- 不在日志中打印密钥。

## Tool Security
每次工具调用校验调用身份、工具注册、JSON Schema、服务端 Policy、超时和配额，并记录审计事件。敏感操作引入人工审批。模型只能提出调用请求，不能授予自己权限。

## Sandbox Security
独立隔离环境；非 root；只读根文件系统；独立 workspace；CPU、内存、PID、磁盘和时间限制；默认禁网；禁止挂载宿主机 Docker Socket；不暴露云凭证和元数据服务；执行后清理。

容器、gVisor 或 Firecracker 提供隔离边界。Rust 负责生命周期和资源控制，不单独构成安全边界。

生产上线前补齐身份认证、OIDC、RBAC/ABAC、KMS、审计防篡改、限流、依赖扫描、镜像扫描和渗透测试。

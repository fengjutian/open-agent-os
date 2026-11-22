# 安全基线

## V0.1
- 模型 API Key 只从后端环境变量读取。
- CORS 限制来源。
- 不开放任意 Shell/Python/Node 执行接口。
- 不把模型输出视为可信指令。
- 不在日志中打印密钥。

## Tool Security
每次工具调用校验调用身份、工具注册、JSON Schema、服务端 Policy、超时和配额，并记录审计事件。敏感操作引入人工审批。模型只能提出调用请求，不能授予自己权限。

### V0.2 已实现
- **deny-by-default**：没有 allow 规则命中就拒绝，规则由服务端维护，模型无法写入。
- 固定顺序：注册表 → JSON Schema → Policy → 审计 → 执行；前一步不过不进入下一步。
- 只向模型暴露**已被放行**的工具（未放行的连 schema 都看不到）。
- 注册工具不产生执行能力：只有内建白名单中的实现可执行，`executor` 类型一律拒绝。
- 每次调用写 `ToolInvocation`，并至少写一条 `AuditLog`；审计只记参数键名，不记参数值。
- 工具超时通过 `timeout_seconds` 控制；实现内部异常不向调用方泄露细节。

### 尚未实现
人工审批、审计防篡改、跨租户隔离。

### 限流与配额（已实现）
- 每密钥每分钟请求数（`RATE_LIMIT_PER_MINUTE`）与每 24 小时运行数（`DAILY_RUN_QUOTA`）。
- 超限返回 429 并带 `Retry-After`；失败运行同样计数，避免靠制造失败绕过。
- 后端可切换：`memory`（进程内）或 `redis`（ZSET 滑动窗口，多副本共享）。
- PostgreSQL 与 SQLite 共用同一套 Alembic 迁移；`render_as_batch` 仅对 SQLite 开启。

## 认证与授权（V0.2）

### 已实现
- **认证默认开启**：除 `/health`、`/docs`、`/openapi.json` 外，全部接口要求 API Key
  （`Authorization: Bearer <key>` 或 `X-API-Key`）。
- 密钥只存 SHA-256 摘要；明文仅在创建响应中出现一次。
- 三角色 RBAC：`viewer`（只读）/ `developer`（写业务数据 + 调用已放行工具）/ `admin`（全部）。
  权限矩阵写死在代码中，不提供「改权限」接口。
- 权限在**服务端**校验，前端隐藏按钮不构成任何保护。
- 不允许停用最后一个可用的 admin 密钥，避免自锁。
- 审计记录操作主体（`key:<id>`），可追溯到具体密钥。
- `AUTH_ENABLED=false` 是本地开发的逃生口，等价于匿名管理员，生产必须保持默认值。

### 仍未实现
OIDC/SSO、组织与多租户、密钥轮换与过期策略、细粒度资源级授权（当前是全局角色）、
登录审计与异常检测。

> 密钥引导：未配置 `BOOTSTRAP_ADMIN_API_KEY` 时，首次启动会自动生成管理员密钥并
> 打印到日志。**日志因此包含一个有效凭据**，请限制日志访问范围，并在生产环境显式配置
> `BOOTSTRAP_ADMIN_API_KEY` 以避免密钥出现在日志中。

## Sandbox Security
独立隔离环境；非 root；只读根文件系统；独立 workspace；CPU、内存、PID、磁盘和时间限制；默认禁网；禁止挂载宿主机 Docker Socket；不暴露云凭证和元数据服务；执行后清理。

容器、gVisor 或 Firecracker 提供隔离边界。Rust 负责生命周期和资源控制，不单独构成安全边界。

生产上线前补齐身份认证、OIDC、RBAC/ABAC、KMS、审计防篡改、限流、依赖扫描、镜像扫描和渗透测试。

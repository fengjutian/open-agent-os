# 安全基线

## V0.1
- 模型 API Key 只从后端环境变量读取。
- CORS 限制来源。
- 不开放任意 Shell/Python/Node 执行接口；V0.3 起只通过受治理的 `executor` 工具、
  在 Docker 容器内执行，且默认关闭。
- 不把模型输出视为可信指令。
- 不在日志中打印密钥。

## Tool Security
每次工具调用校验调用身份、工具注册、JSON Schema、服务端 Policy、超时和配额，并记录审计事件。敏感操作引入人工审批。模型只能提出调用请求，不能授予自己权限。

### V0.2 已实现
- **deny-by-default**：没有 allow 规则命中就拒绝，规则由服务端维护，模型无法写入。
- 固定顺序：注册表 → JSON Schema → Policy → 审计 → 执行；前一步不过不进入下一步。
- 只向模型暴露**已被放行**的工具（未放行的连 schema 都看不到）。
- 注册工具不产生执行能力：`builtin` 只在白名单内执行，`mcp` 只能引用运维声明的服务器，
  `executor` 必须经过 Docker 且默认关闭。
- 每次调用写 `ToolInvocation`，并至少写一条 `AuditLog`；审计只记参数键名，不记参数值。
- 工具超时通过 `timeout_seconds` 控制；实现内部异常不向调用方泄露细节。

### MCP（V0.2）
- MCP 服务器的**启动命令只能由运维通过环境变量声明**，API 无法写入 `command`。
  数据库里只保存服务器名，避免「注册工具」变成任意代码执行。
- MCP 工具与内建工具走同一条治理链：注册表 → JSON Schema → Policy → 审计 → 执行，
  默认拒绝同样适用。
- 每次调用起独立子进程、用完即关；stderr 丢弃以防管道写满死锁；被取消时也会收进程。
  （现已改为按服务器复用连接池：同一服务器串行化、空闲回收、协议错误即丢弃重建。）
- 已知风险：MCP 服务器本身是外部进程，其行为不受本平台约束；只能对它做调用层治理。

### Skill（V0.2）
- 技能内容会被拼进 system prompt，属于**高信任输入**：只有 admin（`write:skills`）能写入。
- 技能内容计入 `SKILL_MAX_CHARS`，避免个别技能挤占上下文预算。
- 停用/缺失的技能在运行时跳过，不会静默改变已有 Agent 的行为而不留痕迹——
  生效列表记录在 `run.started` 事件中。

### 隔离执行器（V0.3）

- 命令只能来自调用参数，并完整经过 JSON Schema → Policy → 审计；注册工具时
  `config` 里不允许写死 `command` / `argv`，避免「注册即获得命令通道」。
- 镜像与运行时由工具定义约束；镜像名做白名单字符校验且不能以 `-` 开头，
  防止被 Docker 当成命令行选项（例如 `--privileged`）。
- 固定隔离参数：`--network none`、`--read-only` + `--tmpfs /tmp`、
  `--cap-drop ALL`、`--security-opt no-new-privileges`、`--pids-limit`、
  `--memory`/`--memory-swap`、`--cpus`；只挂载本次 workspace，不挂 docker socket。
- 默认非 root：Python 侧把后端进程的 `uid:gid` 传给容器，workspace 的 0700 权限
  不需要放宽；Windows 上 Docker Desktop 的 uid 映射不可靠，默认 `0:0`，强度弱于
  Linux 非 root，应显式设置 `EXECUTOR_USER`。直接调用 Rust 时默认 `65534:65534`
  （Windows `0:0`）。
- 超时先 `docker kill` 再 `rm -f`，避免容器残留；stdout/stderr 按字节截断。
- 产物只从 `/workspace/output` 收集普通文件，不跟随符号链接，拒绝 `../`、绝对路径
  和盘符；落盘时再次做包含性校验，SHA-256 在 Python 侧重算。
- 产物下载固定 `application/octet-stream` + attachment，避免用户内容在 API origin
  下被当 HTML 渲染。

**边界**：Docker 容器共享宿主机内核，隔离强度取决于 Docker 与内核配置，不是
gVisor / Firecracker 级别的强隔离。执行器进程被攻破时，攻击面是运行它的宿主机账号
与 Docker 权限，因此不要让它与不可信负载共享宿主机。

### 大 payload 旁路（V0.3）

- 旁路只影响"给模型看什么"，**不影响治理结论**：被拒绝、失败、越权的调用一律不剥离，
  这些信息本来就该完整回到模型。
- `artifact.read` 走完整治理链（Schema → Policy → 审计 → 执行），不是后门。
- **跨租户读不到**：取回靠 `invocation_id`，而 `tool_invocations` 属于 `TenantScoped`，
  由 ORM 事件强制过滤；别人的 id 查不到（表现为 `tool_not_found`，不泄露"是否存在"）。
- 审计只记参数**键名**（`argument_keys`），不记 `invocation_id` 的值，也不记结果内容。
- 平台内建工具按租户懒注册；管理员可以停用它（已存在就不再重建），
  也可以用 deny 规则显式禁掉（默认放行只发生在"没有任何规则命中"时）。

### 尚未实现
按工具调用配额、输入文件与网络白名单、审计外部锚定。

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

### 多租户隔离（V0.2 已实现）
- 所有业务表带 `tenant_id`；租户来自 API Key 的归属或 OIDC 的 tenant claim。
- 隔离在数据访问层统一施加（SQLAlchemy `with_loader_criteria`），覆盖 `select`、
  `Session.get`、`count` 与显式 where，因此不会因为「某个查询忘了加条件」而泄露。
- 跨租户访问统一返回 404，不泄露资源是否存在。
- 唯一需要跨租户读的地方（按密钥哈希认证）必须显式
  `execution_options(include_all_tenants=True)`，绕过行为在代码中可见。
- 测试同时覆盖 ORM 层与 API 层，并有一条结构性测试要求所有模型都有 `tenant_id`。

### 可观测性数据（V0.2）
- span 属性只放标识与计数（租户、agent、run、状态、字符数），
  **不放 prompt、消息内容或任何密钥**——链路后端通常比业务库更容易被更多人看到。
- 指标保持低基数：不把工具名、用户 id 之类会持续增长的维度放进指标属性。
- 日志已包含 `trace_id`/`span_id`，与链路互跳；日志脱敏原则同样适用。

### 组织与成员（V0.2 已实现）
- 租户有 `status`；停用后该租户的所有请求立即 403，恢复后即刻可用。
- 成员的 role **覆盖** IdP 声称的角色：平台可以按租户降权或提权，
  不必改 IdP 配置。`OIDC_REQUIRE_MEMBERSHIP=true` 可强制"必须先邀请"。
- 为不存在的租户签发密钥会被拒绝（404），避免出现"孤立密钥"。
- 已知取舍：**任何 admin 都可以开通新租户**，没有平台级超管角色。
  自托管可接受；多组织托管场景需要额外的平台管理员概念。

### 审计防篡改（V0.2 已实现）
- 审计日志只追加：没有更新/删除接口，ORM 层对 UPDATE/DELETE 直接抛错。
- 每条记录带 `(seq, prev_hash, entry_hash)`，按租户构成哈希链；
  `GET /audit-logs/verify` 能指出首个断裂点（改内容 / 删中间 / 插伪造都会暴露）。
- **边界**：删除尾部记录检测不到（剩余链仍自洽）；拥有数据库完整写权限的人
  可以重算整条链。真正的防篡改需要外部锚定（WORM 存储 / 透明日志 /
  定期把链头写入不可变介质），当前实现只保证「局部改动一定被发现」。

### 流式与断线续传（V0.2）
- 运行在独立任务中执行，客户端断开不会终止它（避免半途状态与"重连即重跑"）。
- 回放端点只读已落库事件，且受租户过滤与 `read` 权限约束。
- SQLite 开启 WAL，使运行任务写、回放端点读不互相阻塞。
- 增量文本不落库：这是有意的取舍（避免把每个 token 写进库），
  代价是重连拿不到断流前已显示的中间文本。

### 仍未实现
跨租户管理员视图、计费、人工审批。

### OIDC / JWT（V0.2 已实现）
- 与 API Key 并存；凭据含两个点按 JWT 校验，否则按 API Key 查库。
- 只信任 IdP 的 JWKS（TTL 缓存 + 未知 kid 强制刷新），本地不保存公钥。
- 校验签名、`iss`、`aud`、`exp`、`nbf`；`sub/exp/iss/aud` 为必需字段。
- 角色取自 claim 且只接受已定义角色，非法值降级为默认角色（默认 `viewer`）。
- 局限：只做认证与租户归属；没有跨租户管理员视图。

> 密钥引导：未配置 `BOOTSTRAP_ADMIN_API_KEY` 时，首次启动会自动生成管理员密钥并
> 打印到日志。**日志因此包含一个有效凭据**，请限制日志访问范围，并在生产环境显式配置
> `BOOTSTRAP_ADMIN_API_KEY` 以避免密钥出现在日志中。

## Sandbox Security（V0.3 已落地）

独立隔离环境；非 root（Windows 例外）；只读根文件系统；独立 workspace；
CPU、内存、PID、输出与总时间限制；默认禁网；不挂宿主机 Docker Socket；
不暴露云凭证与元数据服务；执行后清理 workspace 与容器。

容器 / gVisor / Firecracker 提供隔离边界。Rust 负责生命周期和资源控制，
不单独构成安全边界。当前只实现 Docker 后端，gVisor / Firecracker 待评估。

上线前仍需补齐：KMS 与密钥轮换、镜像与依赖扫描、成本控制、渗透测试，
以及更强的执行隔离。

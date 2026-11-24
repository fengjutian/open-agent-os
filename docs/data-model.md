# 数据模型

## V0.1
- Agent：id、name、description、system_prompt、model、created_at、updated_at。
- Conversation：id、agent_id、title、created_at、updated_at。
- Message：id、conversation_id、role、content、created_at。

关系：Agent 1:N Conversation；Conversation 1:N Message。

## V0.2 工具治理
- ToolDefinition：id、name（唯一）、description、input_schema（JSON Schema）、permissions、timeout_seconds、execution_type、config、enabled、created_at、updated_at。

`execution_type`：`builtin`（平台内建实现）/ `mcp`（转发已声明的 MCP 服务器）/
`executor`（Docker 隔离执行）。`config` 在 MCP 场景下形如
`{"server": "<已声明的服务器名>", "tool": "<远程工具名>"}`——只存服务器名，
启动命令留在环境变量里；executor 场景下形如
`{"runtime": "shell|python", "image": "<镜像>", "command_argument": "command"}`，
**不允许**存命令本体，命令只能来自调用参数。
- PolicyRule：id、agent_id（可空=全局）、tool_name（或 `*`）、effect（allow/deny）、priority、enabled、note、created_at。
- ToolInvocation：id、run_id、agent_id、conversation_id、tool_name、arguments、result、status、error_code、error_message、started_at、finished_at。
- AuditLog：id、run_id、request_id、actor、action、resource、decision、reason、detail、created_at，
  外加 `seq` / `prev_hash` / `entry_hash` 构成按租户的哈希链（只追加，ORM 拒绝改动）。

关键取舍：**ToolInvocation 与 AuditLog 不设外键**，避免随 Agent 删除而级联消失——
审计记录应当比业务数据活得久。`agent_id` 仅作为普通带索引列保存。

关系：Agent 1:N PolicyRule（agent_id 为空则是全局规则）。

## V0.2 运行时
- Run：id（即 run_id）、agent_id、conversation_id、key_id、status、model、iterations、tool_calls、prompt_chars、answer_chars、error_code、error_message、started_at、finished_at。
- RunEvent：id、run_id、seq、type、payload（JSON）、created_at。追加写，不改。

`seq` 在单次运行内严格递增，便于还原执行顺序。事件类型：
`run.started`、`context.trimmed`、`model.request`、`model.response`、`tool.result`、`run.completed`、`run.failed`。

同样不建外键：事件寿命不依赖业务数据生命周期。

## V0.2 认证
- ApiKey：id、name（唯一）、key_prefix、key_hash（SHA-256，唯一索引）、role、enabled、created_at、last_used_at。

只存哈希；`key_prefix` 仅用于列表展示辨识，不参与鉴权。角色是全局的，
不做资源级授权；租户归属由 `tenant_id` 决定（见下文「多租户」）。

## V0.2 技能
- Skill：id、name（唯一）、description、instructions、enabled、created_at、updated_at。
- Agent.skill_names：引用的技能名列表（JSON）。

刻意用**名称**而不是外键：运行时按名称解析，缺失/停用就跳过，
避免改了技能就把历史 Agent 卡住；代价是重命名会断引用，因此写入时校验存在性。

## V0.3 执行产物
- ToolArtifact：id、tenant_id、invocation_id、run_id、tool_name、path（相对 output 的路径）、
  size_bytes、sha256、created_at。
  唯一约束 `(tenant_id, invocation_id, path)`；`invocation_id` 与 `run_id` 带索引。
- 文件本体落在 `ARTIFACT_DIR/<tenant>/<invocation_id>/<path>`，数据库只存索引。
  删除记录不会自动删文件。

`path` 统一用 `/` 分隔；写入与读取都会校验它解析后仍在对应目录内，符号链接一律不读。
`size_bytes` / `sha256` 以宿主机重新读取的结果为准，执行器上报的清单只当索引。

## 多租户
所有业务表（agents、conversations、messages、tool_definitions、policy_rules、
tool_invocations、tool_artifacts、audit_logs、api_keys、runs、run_events、skills、
memberships）都有 `tenant_id`（默认 `default`，带索引）。

它们都继承抽象映射基类 `TenantScoped`——租户过滤通过
`with_loader_criteria(TenantScoped, ...)` 精确作用于这些实体，而不会波及全局表。

- `Tenant`：id（slug）、name、status（active/disabled）、created_at、updated_at。
  **不**继承 TenantScoped——它就是隔离边界本身。
- `Membership`：(tenant_id, subject) 唯一，subject 与 `Principal.key_id` 对齐，
  带 role。用于按租户覆盖 IdP 声称的角色。

唯一约束从「全局唯一」改为「租户内唯一」：`tool_definitions` 与 `skills` 使用
`(tenant_id, name)`。`api_keys.name` 保持全局唯一，`api_keys.key_hash` 全局唯一
（一把密钥只属于一个租户）。

过滤由 `app/db/tenancy.py` 的 ORM 事件统一施加，不依赖每条查询自觉过滤。

## 企业化扩展
AgentVersion、Checkpoint、Secret、CostRecord 等仍未实现。
（ToolDefinition / PolicyRule / ToolInvocation / AuditLog / Skill / Tenant / Membership /
ToolArtifact 已落地，见上。）

多租户表应引入 tenant_id，并通过服务端鉴权与数据库约束双重隔离。PostgreSQL RLS 可作为纵深防御，不能只信任前端传来的 tenant_id。

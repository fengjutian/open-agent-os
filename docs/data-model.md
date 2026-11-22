# 数据模型

## V0.1
- Agent：id、name、description、system_prompt、model、created_at、updated_at。
- Conversation：id、agent_id、title、created_at、updated_at。
- Message：id、conversation_id、role、content、created_at。

关系：Agent 1:N Conversation；Conversation 1:N Message。

## V0.2 工具治理
- ToolDefinition：id、name（唯一）、description、input_schema（JSON Schema）、permissions、timeout_seconds、execution_type、enabled、created_at、updated_at。
- PolicyRule：id、agent_id（可空=全局）、tool_name（或 `*`）、effect（allow/deny）、priority、enabled、note、created_at。
- ToolInvocation：id、run_id、agent_id、conversation_id、tool_name、arguments、result、status、error_code、error_message、started_at、finished_at。
- AuditLog：id、run_id、request_id、actor、action、resource、decision、reason、detail、created_at。

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
不做资源级授权，也没有组织/租户维度。

## 企业化扩展
Organization、User、Membership、Role、Permission、ApiKey、AgentVersion、Run、RunEvent、
Checkpoint、ExecutionArtifact、Secret。（ToolDefinition / PolicyRule / ToolInvocation /
AuditLog 已在 V0.2 落地，见上。）

多租户表应引入 tenant_id，并通过服务端鉴权与数据库约束双重隔离。PostgreSQL RLS 可作为纵深防御，不能只信任前端传来的 tenant_id。

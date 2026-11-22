# 数据模型

## V0.1
- Agent：id、name、description、system_prompt、model、created_at、updated_at。
- Conversation：id、agent_id、title、created_at、updated_at。
- Message：id、conversation_id、role、content、created_at。

关系：Agent 1:N Conversation；Conversation 1:N Message。

## 企业化扩展
Organization、User、Membership、Role、Permission、ApiKey、AgentVersion、ToolDefinition、PolicyRule、Run、RunEvent、Checkpoint、ToolInvocation、ExecutionArtifact、AuditLog、Secret。

多租户表应引入 tenant_id，并通过服务端鉴权与数据库约束双重隔离。PostgreSQL RLS 可作为纵深防御，不能只信任前端传来的 tenant_id。

# API 契约 V0.1

Base URL：`/api/v1`

- `POST /agents`：创建 Agent，body 包含 name、description、system_prompt、model。
- `GET /agents`：Agent 列表。
- `GET /agents/{agent_id}`：Agent 详情。
- `PATCH /agents/{agent_id}`：更新 Agent。
- `DELETE /agents/{agent_id}`：删除 Agent。
- `POST /agents/{agent_id}/conversations`：创建会话，body `{ "title": "New conversation" }`。
- `GET /agents/{agent_id}/conversations`：会话列表。
- `GET /conversations/{conversation_id}/messages`：消息历史。
- `POST /conversations/{conversation_id}/messages`：发送消息，body `{ "content": "..." }`。
- `GET /health`：健康检查。

发送消息返回 user_message 和 assistant_message。后续版本增加 run_id、SSE 事件、统一错误结构、幂等键和任务取消。

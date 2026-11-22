# API 契约 V0.1

Base URL：`/api/v1`

## 通用约定

### 分页

所有列表接口统一返回分页信封：

```json
{ "items": [], "total": 0, "limit": 50, "offset": 0 }
```

查询参数：

| 参数 | 默认 | 约束 |
|---|---|---|
| `limit` | 50 | 1–200，越界返回 422 |
| `offset` | 0 | ≥ 0 |

### 错误

所有错误统一返回：

```json
{
  "error": {
    "code": "not_found",
    "message": "Agent not found",
    "request_id": "req_0ce0bda1701c4e4a8bcdb0db96a7b0ce",
    "details": null
  }
}
```

- `code` 稳定，客户端应按它分支；`message` 仅供展示。
- `request_id` 与响应头 `X-Request-ID` 一致；客户端也可主动传入该请求头以串联链路。
- 未预期异常只返回 `internal_error`，不泄露堆栈或上游响应体。

| code | HTTP | 含义 |
|---|---|---|
| `not_found` | 404 | 资源不存在 |
| `validation_error` | 422 | 请求体或查询参数校验失败（`details` 为字段级错误） |
| `model_not_configured` | 424 | 缺少模型密钥等配置 |
| `model_provider_error` | 502 / 504 | 模型服务错误或超时 |
| `internal_error` | 500 | 未预期异常 |

## 接口

- `POST /agents`：创建 Agent，body 包含 name、description、system_prompt、model。
  省略 `model` 时取 `MODEL_NAME` 配置。
- `GET /agents`：Agent 列表（分页）。
- `GET /agents/{agent_id}`：Agent 详情。
- `PATCH /agents/{agent_id}`：更新 Agent。
  未传的字段保持不变；`name`/`system_prompt`/`model` **不可显式传 null**（返回 422），
  `description` 可传 null 以清空。
- `DELETE /agents/{agent_id}`：删除 Agent，级联删除其会话与消息，返回 204。
- `POST /agents/{agent_id}/conversations`：创建会话，body `{ "title": "New conversation" }`。
- `GET /agents/{agent_id}/conversations`：会话列表（分页），按最近活动倒序。
- `GET /conversations/{conversation_id}/messages`：消息历史（分页），按时间正序。
- `POST /conversations/{conversation_id}/messages`：发送消息，body `{ "content": "..." }`。
  响应：

```json
{
  "run_id": "run_d392dd62d4ae42cf8b69bc39e6045b82",
  "user_message": { "...": "" },
  "assistant_message": { "...": "" }
}
```

  `run_id` 标识本次 Agent 运行，与后端日志中的 `run_id` 字段对应，便于逐次排查。
  模型调用失败时整次运行回滚，不会留下半截的用户消息。
- `GET /health`：健康检查。

## 时间字段

所有时间均为 UTC，序列化为带 `Z` 后缀的 ISO 8601，例如 `2026-11-22T12:37:54.123456Z`。
（SQLite 不保存时区，服务端会在输出时补上 UTC 标记。）

## 后续版本

后续增加 SSE 事件流、Run/Event 持久化、幂等键与任务取消。

# 功能文档索引

本目录存放**功能级**文档：设计稿、对标清单、实现说明。总体架构与接口契约在上一层
（`architecture.md` / `api-contract.md` / `data-model.md` / `security.md`）。

## 状态一览

| 功能 | 状态 | 文档 | 提交 |
|---|---|---|---|
| 会话模式 Ask / Plan / Craft | ✅ 已实现 | [conversation-modes.md](conversation-modes.md) | `68287ff` |
| 定时任务 cron | ✅ 已实现 | [cron.md](cron.md) | `b99d2b2` |
| 人工审批 HITL | ✅ 已实现 | [hitl.md](hitl.md) | `e5bf6e3` |
| 上下文压缩（历史摘要） | ✅ 已实现 | [context-summary.md](context-summary.md) | `072c456` |
| 长期记忆 | ✅ 已实现 | [long-term-memory.md](long-term-memory.md) | `0156a28` |
| 子代理 | ✅ 已实现 | [subagents.md](subagents.md) | `3d4cd95` |
| 知识库 RAG | ✅ 已实现 | [knowledge-base.md](knowledge-base.md) | 本次提交 |
| 工具结果大 payload 旁路 | 📝 设计稿 | [tool-result-offload.md](tool-result-offload.md) | — |
| Octop 功能全景（对齐基线） | 📚 参考 | [octop-feature-inventory.md](octop-feature-inventory.md) | `414db2d` |

## 已实现能力（本仓）

按能力域列出，便于回答「这个项目现在能做什么」：

| 域 | 能力 | 关键位置 |
|---|---|---|
| 对话 | 会话/消息、会话模式三档 | `api/routes.py`、`services/conversation_modes.py` |
| Agent | CRUD、技能绑定 | `api/routes.py`、`services/skills.py` |
| 工具治理 | 注册 → JSON Schema → policy → 审计 → 执行；内建白名单 | `services/tool_gateway.py`、`tool_executor.py` |
| MCP | stdio 客户端、连接池、串行化、空闲回收 | `services/mcp_client.py`、`mcp_pool.py` |
| 运行 | 运行记录、事件流、SSE、断线续传 | `api/run_routes.py`、`services/run_stream.py` |
| 定时任务 | cron 表达式、进程内调度、执行记录 | `services/cron.py`、`cron_scheduler.py`、`api/cron_routes.py` |
| 人工审批 | 工具挂起、批准/拒绝、检查点续跑 | `services/approvals.py`、`tool_gateway.py`、`api/approval_routes.py` |
| 上下文压缩 | 历史摘要替代整条丢弃、增量摘要 | `services/summarizer.py`、`agent_runtime.py`、`context.py` |
| 长期记忆 | 词法召回、按需注入、会话提炼 | `services/memory.py`、`api/memory_routes.py` |
| 子代理 | 派生完整子运行、深度上限、结果回灌 | `services/subagents.py`、`tool_gateway.py` |
| 知识库 | 切块、向量化、词面门控检索、工具接入 | `services/knowledge.py`、`embeddings.py`、`api/knowledge_routes.py` |
| 认证 | API Key、OIDC/JWT、三角色 RBAC | `services/api_keys.py`、`core/security.py` |
| 多租户 | 数据层强制隔离、组织与成员、成员覆盖 IdP 角色 | `db/tenancy.py`、`services/tenants.py` |
| 限流 | 每分钟限流、日配额（内存 / Redis） | `services/rate_limit.py` |
| 审计 | 只追加 + 哈希链 | `services/audit_chain.py` |
| 可观测 | OpenTelemetry trace/metrics/log 关联 | `core/telemetry.py` |
| 评测 | 评测 runner 与套件 | `evals/` |
| 前端 | 四视图、模式切换、窄屏适配 | `frontend/src/` |

## 待办（按不依赖外部服务优先）

| 批次 | 功能 | 依赖 |
|---|---|---|
| B3 | 用量报表 | 无 |

完整差距清单见 [octop-feature-inventory.md](octop-feature-inventory.md)。

## 写新功能文档的约定

1. 文件名用 kebab-case，与功能同名；
2. 开头写明**对标对象**与**状态**（设计稿 / 已实现）；
3. 必须有「问题 → 根因 → 方案 → 边界」四段，避免只写"怎么做"而不写"为什么"；
4. 引用代码时写 `file:line`，并在实现后回来核对行号；
5. 明确列出「不做什么」，比列出"要做什么"更能防止范围膨胀；
6. 实现完成后把状态改为「已实现」，并把提交号填进上表。

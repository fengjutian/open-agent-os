# 产品需求文档 PRD

## 1. 产品定位
自托管企业级 Agent Platform，提供 Agent 创建、运行、工具治理、工作流、安全执行和可观测能力。不是单一聊天机器人，而是 Agent 的开发、运行和治理平台。

## 2. 用户
- 平台管理员：模型、Agent、用户、权限、运行管理。
- Agent 开发者：Prompt、工具、调试、发布。
- 企业业务用户：使用已发布 Agent。
- 运维与安全人员：运行记录、异常、审计、成本。

## 3. 技术原则
1. Harness-owned：平台掌控 Agent Loop、上下文、工具调用和策略。
2. Model-agnostic：模型通过 Provider Adapter 接入。
3. Workflow + Agent：确定性流程交给 Workflow，自主决策交给 Agent Loop。
4. Secure by design：工具权限在服务端校验；不可信代码进入隔离环境。
5. Observable：关键步骤以事件记录。
6. Self-hostable：支持本地和私有化部署。

## 4. V0.1 功能范围
### P0
- Agent 创建、列表、详情、更新、删除。
- Agent 配置：名称、描述、System Prompt、模型名称。
- 会话创建、会话列表、消息历史。
- 发送消息并返回 Agent 回复。
- 消息持久化。
- Mock Provider 和 OpenAI-compatible Provider。
- React 管理界面与聊天界面。
- 健康检查、OpenAPI 文档。

### 暂不实现
用户认证、多租户、RBAC/ABAC、MCP、Agent Teams、长期记忆、向量检索、可视化 Workflow、Temporal、Rust Sandbox、计费和完整 OTel。

## 5. 后续功能需求
### FR-Tool
ToolDefinition 包含 name、description、input_schema、permissions、timeout、execution_type。调用必须经过工具存在性校验、Schema 校验、服务端 Policy 校验、审计，再执行。

> V0.2 已实现该链路（含 deny-by-default 策略与审计）；人工审批与调用配额仍待做。

### FR-Workflow
支持顺序、并行、条件、循环、人工审批、重试和 Checkpoint。React Flow 负责编辑展示，后端负责执行。

### FR-Sandbox
Rust Executor 管理隔离环境生命周期、资源限制和产物收集。隔离由容器、gVisor 或 Firecracker 等机制提供，Rust 进程本身不构成安全边界。

### FR-Observability
统一 run_id、trace_id、事件、耗时、错误、重试、Token 和成本；敏感信息脱敏。

## 6. 非功能需求
模块化单体起步；Provider、Tool、Storage 可插拔；API 使用 REST/OpenAPI，后续增加 SSE；SQLite 仅开发使用，生产 PostgreSQL；任务可靠性后续接入 Temporal。

## 7. V0.1 验收标准
1. 无模型密钥可启动前后端。
2. 可以创建 Agent 和会话。
3. 发送消息可获得 Mock 回复。
4. Agent、会话、消息重启后仍存在。
5. 配置兼容模型 API 后可真实调用。
6. `/health` 正常，`/docs` 可调试。

## 8. 主流程
用户创建 Agent → 创建 Conversation → 发送 Message → Runtime 读取 Agent 与历史 → Model Gateway 调用 Provider → 保存回复 → 前端展示。

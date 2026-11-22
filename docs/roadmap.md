# Roadmap

## V0.1 Agent Core
- Agent CRUD、Conversation、消息持久化
- Model Provider 抽象
- Mock / OpenAI-compatible Provider
- React 管理和聊天界面
- SSE、测试和统一错误后续补齐

## V0.2 Harness
- Tool Registry、JSON Schema 校验、Policy Engine
- Tool Call Loop、MCP
- Context Compaction、Skill System
- Run / Event 持久化、Evaluation

## V0.3 Execution
- Rust Executor、Docker Sandbox
- 超时取消、资源限制、文件产物
- 评估 gVisor / Firecracker

## V0.4 Enterprise
- OIDC、组织与多租户、RBAC/ABAC
- PostgreSQL、Redis、Temporal
- 审计、限流、配额、成本管理
- OpenTelemetry

## V1.0 Platform
- Agent Studio、React Flow Workflow Editor
- Agent 发布和版本、多 Agent 协作
- SDK / CLI、Webhook、私有化部署

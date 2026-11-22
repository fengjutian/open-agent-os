# Roadmap

## V0.1 Agent Core
- Agent CRUD、Conversation、消息持久化
- Model Provider 抽象
- Mock / OpenAI-compatible Provider
- React 管理和聊天界面
- SSE、测试和统一错误后续补齐

## V0.2 Harness
- ✅ Tool Registry、JSON Schema 校验、Policy Engine（deny-by-default）
- ✅ Tool Call Loop（轮次上限 4）、审计事件与工具调用记录
- ✅ Context Compaction（按字符预算丢弃最旧历史）
- ✅ Run / RunEvent 持久化 + 事件流查询
- ✅ 限流与每日运行配额
- 待办：MCP、Skill System、Evaluation

## V0.3 Execution
- Rust Executor、Docker Sandbox
- 超时取消、资源限制、文件产物
- 评估 gVisor / Firecracker

## V0.4 Enterprise
- ⏳ OIDC、组织与多租户、RBAC/ABAC
  （已提前落地：API Key 认证 + admin/developer/viewer 三角色 RBAC）
- PostgreSQL、Redis、Temporal
- 审计、限流、配额、成本管理
- OpenTelemetry

## V1.0 Platform
- Agent Studio、React Flow Workflow Editor
- Agent 发布和版本、多 Agent 协作
- SDK / CLI、Webhook、私有化部署

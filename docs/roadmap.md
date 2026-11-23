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
- ✅ SSE 流式输出
- ✅ Evaluation（JSON 数据集 + 断言 + CLI，可进 CI）
- ✅ MCP（stdio 客户端 + 同步 + 仍走治理链）
- ✅ Skill System（指令包 + 运行时注入 + 预算截断）

## V0.3 Execution
- Rust Executor、Docker Sandbox
- 超时取消、资源限制、文件产物
- 评估 gVisor / Firecracker

## V0.4 Enterprise
- ⏳ OIDC、组织与多租户、RBAC/ABAC
  （已落地：API Key + OIDC/JWT 认证、三角色 RBAC、数据层多租户隔离、
  租户生命周期与成员关系；平台级超管与计费仍未实现）
- ⏳ PostgreSQL、Redis（已支持：PG 语法与迁移、Redis 限流后端）
- Temporal
- 审计、限流、配额、成本管理（限流与配额已落地）
- OpenTelemetry（已落地：链路、3 个指标、日志 trace 关联；默认关闭）

## V1.0 Platform
- Agent Studio、React Flow Workflow Editor
- Agent 发布和版本、多 Agent 协作
- SDK / CLI、Webhook、私有化部署

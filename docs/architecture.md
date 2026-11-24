# 架构设计

> **本文描述的是目标架构，不是当前实现的完整清单。** 已落地：REST API、Agent Runtime、
> Tool Registry / Policy Engine / MCP / Skill、SSE 与断线续传、Run/RunEvent、评测、
> 多租户与 OIDC、PostgreSQL、Redis 限流、OpenTelemetry，以及 Rust Executor 的
> Docker 后端（V0.3 竖切面）。仍未接入：Workflow Engine、Temporal、Qdrant、MinIO、
> gVisor / Firecracker。当前能力清单以 README 为准。

## 总体结构
```text
React + TypeScript
  └─ Dashboard / Agent Studio / Workflow Editor / SDK / CLI
             │ REST / SSE
Python + FastAPI
  ├─ API Layer
  ├─ Agent Runtime
  ├─ Model Gateway
  ├─ Tool Registry
  ├─ Context / Memory
  ├─ Workflow Engine
  └─ Policy Engine
             │ 内部 RPC / Queue（后续）
Rust Executor
  ├─ Sandbox Manager
  ├─ Process Manager
  ├─ Filesystem
  └─ Resource Limits

基础设施：PostgreSQL / Redis / Temporal / Qdrant / MinIO / OpenTelemetry

> 其中 **OpenTelemetry、PostgreSQL、Redis、Docker 执行器已落地**；Temporal / Qdrant /
> MinIO / gVisor / Firecracker 仍未接入。
```

## 语言职责
- TypeScript + React：产品界面、可视化编辑器、SDK、CLI。
- Python：Agent Loop、模型路由、工具注册、策略、记忆、工作流和评估。
- Rust：执行器、进程和文件管理、资源限制、Sandbox 生命周期。

## 模块边界
| 模块 | 输入 | 输出 | 实现 |
|---|---|---|---|
| Agent API | HTTP | DTO | Python |
| Agent Runtime | Agent + History + Input | Response / Events | Python |
| Model Gateway | ModelRequest | ModelResponse | Python |
| Tool Registry | ToolCall | Validated Tool | Python |
| Policy Engine | Principal + Action + Resource | Allow / Deny | Python |
| Executor Client | ExecutionRequest | ExecutionResult | Python |
| Sandbox Runtime | ExecutionRequest | ExecutionResult | Rust |
| Studio | 用户操作 | API 请求 | React |

## Runtime 执行流程
校验 Agent 和会话 → 创建 run_id → 读取 Prompt 与历史 → 构建上下文 → 调用模型 → 校验 Tool Call → Policy 授权 → 执行工具 → 回填结果 → 结束或继续 → 保存事件与结果。

V0.2 起实现工具调用循环（轮次上限 `MAX_TOOL_ITERATIONS`）；V0.3 起
`execution_type=executor` 的工具在 Docker 容器中执行，产物落在
`ARTIFACT_DIR/<tenant>/<invocation_id>/`。

## 协议
V0.1 浏览器到后端使用 REST JSON。V0.2 起提供 SSE 推送与断线续传；Python 到 Rust
Executor 使用 **stdin/stdout 上的版本化 JSON**（`schema_version=1`），一次调用一个
执行器进程，超时由 Rust 侧杀容器。跨语言契约采用 OpenAPI / JSON Schema，Python
Pydantic、Rust Serde、TypeScript 生成类型；所有事件带 schema_version。

## 部署演进
开发 SQLite + 单 FastAPI 进程。生产 PostgreSQL；Redis 做缓存、锁、限流；Temporal 管长任务；S3/MinIO 存文件（当前产物先落本地 `ARTIFACT_DIR`）；Qdrant/pgvector 做向量检索；Rust Executor 运行在能访问 Docker daemon 的受控宿主机/runner 上，当前由后端按次拉起子进程。

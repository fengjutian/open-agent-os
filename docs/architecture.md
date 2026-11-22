# 架构设计

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

V0.1 仅实现单次模型调用，不执行工具循环。

## 协议
V0.1 浏览器到后端使用 REST JSON。后续 SSE 推送事件；Python 到 Rust 使用 Unix Domain Socket 或 gRPC。跨语言契约采用 OpenAPI / JSON Schema，Python Pydantic、Rust Serde、TypeScript 生成类型；所有事件带 schema_version。

## 部署演进
开发 SQLite + 单 FastAPI 进程。生产 PostgreSQL；Redis 做缓存、锁、限流；Temporal 管长任务；S3/MinIO 存文件；Qdrant/pgvector 做向量检索；Rust Executor 独立部署。

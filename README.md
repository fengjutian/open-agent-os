# Enterprise Agent Platform

V0.1 starter：React + TypeScript 前端、Python + FastAPI Agent Core，预留 Rust Executor 边界。

## 目录结构

```text
backend/          FastAPI + SQLAlchemy + Alembic（Agent Core）
frontend/         React + TypeScript（Vite）
rust-executor/    Rust Executor —— V0.3 占位，当前拒绝一切执行请求
docs/             PRD / 架构 / 数据模型 / API 契约 / 安全基线 / Roadmap
```

## 本地启动

后端：
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

前端：
```bash
cd frontend
npm ci
npm run dev
```

访问 http://localhost:5173，API 文档 http://localhost:8000/docs。

默认 Mock Provider，不需要模型密钥。配置 backend/.env 的 MODEL_PROVIDER、MODEL_BASE_URL、MODEL_API_KEY 可切换到 OpenAI-compatible API；
MODEL_NAME 是创建 Agent 时未显式指定模型所使用的默认模型。

## Docker Compose 启动

```bash
cp backend/.env.example backend/.env   # 可选：只有接真实模型时才需要
docker compose up --build
```

- 前端 http://localhost:5173，后端 http://localhost:8000/docs。
- `backend/.env` 在 compose 中是**可选**的（`required: false`）。缺失时后端使用代码内默认值，以 Mock Provider 正常启动。
- SQLite 数据落在命名卷 `agent-data` 的 `/app/data/agent_platform.db`，重建容器不丢数据。
  注意：compose 用 `environment` 固定了 `DATABASE_URL`，其优先级高于 `backend/.env`；要换数据库请直接改 `docker-compose.yml`。

## 数据库迁移

Schema 由 Alembic 唯一负责，应用启动时会自动 `upgrade head`（空库即建表）。

```bash
cd backend
alembic upgrade head                                  # 手动升级
alembic revision --autogenerate -m "add something"    # 改完模型后生成迁移
alembic downgrade -1                                  # 回滚一步
```

数据库 URL 来自 `app.core.config.settings`（即 .env / 环境变量），`alembic.ini` 里不再重复配置，
避免两处配置漂移。`tests/test_migrations.py` 会校验「迁移后的结构」与「ORM 模型」完全一致。

## 测试与构建

```bash
cd backend && pip install -r requirements-dev.txt && pytest    # 测试
cd backend && ruff check .                                     # 静态检查
cd frontend && npm run build                                   # tsc -b && vite build
cargo build --manifest-path rust-executor/Cargo.toml
```

CI（`.github/workflows/ci.yml`）会跑：Ruff、pytest、前端构建、Rust 构建，以及 `docker compose config`。

## 错误与分页契约

- 列表接口统一返回 `{ items, total, limit, offset }`，`limit` 默认 50、上限 200。
- 错误统一返回 `{ "error": { code, message, request_id, details } }`，`code` 稳定可分支。
- 每个响应都带 `X-Request-ID`；发消息的响应额外返回 `run_id`。

详见 [docs/api-contract.md](docs/api-contract.md)。

## 可观测性

日志为单行 JSON，默认包含 `request_id`；Agent 运行期间额外带 `run_id`：

```json
{"ts":"...","level":"INFO","logger":"app.api.middleware","message":"request",
 "request_id":"req_...","method":"POST","path":"/api/v1/conversations/.../messages",
 "status":200,"duration_ms":15.98}
```

## 上下文窗口

`CONTEXT_MAX_CHARS`（默认 24000）限制单次请求的上下文预算。超出时从最旧的历史消息开始丢弃，
最新的用户输入始终保留（过长则截断尾部），并在日志中记录 `context trimmed to budget` 事件。

## 工具治理（V0.2）

工具调用固定走五步，任一步不通过都不会进入下一步：

```text
Tool Registry → JSON Schema → Policy Engine → Audit → Execution
```

**默认拒绝。** 注册一个工具不会让它可用，必须显式添加 allow 规则：

```bash
# 1. 注册工具（内建实现只有 echo / current_time）
curl -X POST localhost:8000/api/v1/tools -H 'Content-Type: application/json' -d '{
  "name": "echo",
  "description": "回显文本",
  "input_schema": {"type":"object","properties":{"text":{"type":"string"}},
                   "required":["text"],"additionalProperties":false},
  "permissions": ["read:text"]
}'

# 2. 此时调用会被拒绝（deny-by-default）
curl -X POST localhost:8000/api/v1/agents/<AGENT_ID>/tools/echo/invoke \
  -H 'Content-Type: application/json' -d '{"arguments":{"text":"hi"}}'
# => {"status":"denied","error_code":"policy_denied", ...}

# 3. 显式放行后再调用
curl -X POST localhost:8000/api/v1/policy-rules -H 'Content-Type: application/json' \
  -d '{"agent_id":"<AGENT_ID>","tool_name":"echo","effect":"allow"}'
# => {"status":"succeeded","result":{"text":"hi"}, ...}
```

要点：

- 策略裁决：priority 大者优先 → agent 专属优先于全局 → 同层 deny 优先于 allow。
- **注册 ≠ 有执行能力**：只有内建白名单（`echo`、`current_time`）能真正执行；
  `execution_type=executor` 一律返回 `executor_not_available`，等 V0.3 隔离执行器。
- 每次调用都落 `ToolInvocation` 与 `AuditLog`（审计只记参数键名，不记参数值）。
- 模型只能提出调用请求：Agent Loop 里只会把**已放行**的工具告诉模型，
  被拒绝的结果会连同原因回灌给模型，最多 `MAX_TOOL_ITERATIONS`（默认 4）轮。

接口与状态码见 [docs/api-contract.md](docs/api-contract.md)。

## 依赖锁定

- 前端：`package.json` 使用精确版本，`package-lock.json` 已提交，CI/镜像用 `npm ci`。
- 后端：`requirements.txt`（运行时）与 `requirements-dev.txt`（测试/lint）均使用 `==` 精确版本。
- 升级依赖请显式改版本并同时更新锁文件，不要用 `latest`。

## Rust Executor 现状

`rust-executor` 目前是一个**可编译但拒绝所有执行请求**的占位实现，用来固定 ExecutionRequest / ExecutionResult 的版本化协议形状。详见 [rust-executor/README.md](rust-executor/README.md)。

## 当前能力
- Agent CRUD（创建、列表、详情、更新、删除）
- 创建会话、查看消息、发送消息
- SQLite 持久化 + Alembic 迁移
- Mock / OpenAI-compatible 模型适配器
- 统一错误信封、分页、结构化日志与 request_id/run_id
- 工具注册表 + JSON Schema 校验 + 服务端策略（deny-by-default）+ 审计
- Agent Loop 工具调用（轮次上限可配）
- React Agent 管理和聊天界面

## 重要边界
V0.1 不执行任意 Shell / Python / Node 代码；没有认证、多租户、RBAC、生产级任务恢复。Rust Executor 目前是架构预留，不是可用 Sandbox。生产环境必须补充隔离机制、鉴权、审计、限流和密钥管理。

# Enterprise Agent Platform

V0.1 starter：React + TypeScript 前端、Python + FastAPI Agent Core，预留 Rust Executor 边界。

## 目录结构

```text
backend/          FastAPI + SQLAlchemy（Agent Core）
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

默认 Mock Provider，不需要模型密钥。配置 backend/.env 的 MODEL_PROVIDER、MODEL_BASE_URL、MODEL_API_KEY、MODEL_NAME 可切换到 OpenAI-compatible API。

## Docker Compose 启动

```bash
cp backend/.env.example backend/.env   # 可选：只有接真实模型时才需要
docker compose up --build
```

- 前端 http://localhost:5173，后端 http://localhost:8000/docs。
- `backend/.env` 在 compose 中是**可选**的（`required: false`）。缺失时后端使用代码内默认值，以 Mock Provider 正常启动。
- SQLite 数据落在命名卷 `agent-data` 的 `/app/data/agent_platform.db`，重建容器不丢数据。
  注意：compose 用 `environment` 固定了 `DATABASE_URL`，其优先级高于 `backend/.env`；要换数据库请直接改 `docker-compose.yml`。

## 测试与构建

```bash
cd backend && python -m pytest          # 后端测试
cd frontend && npm run build            # tsc -b && vite build
cargo build --manifest-path rust-executor/Cargo.toml
```

## 依赖锁定

- 前端：`package.json` 使用精确版本，`package-lock.json` 已提交，CI/镜像用 `npm ci`。
- 后端：`requirements.txt` 使用 `==` 精确版本。
- 升级依赖请显式改版本并同时更新锁文件，不要用 `latest`。

## Rust Executor 现状

`rust-executor` 目前是一个**可编译但拒绝所有执行请求**的占位实现，用来固定 ExecutionRequest / ExecutionResult 的版本化协议形状。详见 [rust-executor/README.md](rust-executor/README.md)。

## 当前能力
- Agent CRUD（创建、列表、详情、更新、删除）
- 创建会话、查看消息、发送消息
- SQLite 持久化
- Mock / OpenAI-compatible 模型适配器
- React Agent 管理和聊天界面

## 重要边界
V0.1 不执行任意 Shell / Python / Node 代码；没有认证、多租户、RBAC、生产级任务恢复。Rust Executor 目前是架构预留，不是可用 Sandbox。生产环境必须补充隔离机制、鉴权、审计、限流和密钥管理。

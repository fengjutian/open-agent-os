# Enterprise Agent Platform

V0.1 starter：React + TypeScript 前端、Python + FastAPI Agent Core，预留 Rust Executor 边界。

## 启动

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
npm install
npm run dev
```

访问 http://localhost:5173，API 文档 http://localhost:8000/docs。

默认 Mock Provider，不需要模型密钥。配置 backend/.env 的 MODEL_PROVIDER、MODEL_BASE_URL、MODEL_API_KEY、MODEL_NAME 可切换到 OpenAI-compatible API。

## 当前能力
- Agent CRUD（创建、列表、详情、更新、删除）
- 创建会话、查看消息、发送消息
- SQLite 持久化
- Mock / OpenAI-compatible 模型适配器
- React Agent 管理和聊天界面

## 重要边界
V0.1 不执行任意 Shell / Python / Node 代码；没有认证、多租户、RBAC、生产级任务恢复。Rust Executor 目前是架构预留，不是可用 Sandbox。生产环境必须补充隔离机制、鉴权、审计、限流和密钥管理。

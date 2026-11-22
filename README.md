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

## 认证与权限

**认证默认开启。** 首次启动时，如果没有配置 `BOOTSTRAP_ADMIN_API_KEY`，系统会自动生成一个
管理员密钥并**只在日志里打印一次**：

```json
{"level":"WARNING","logger":"app.main","message":"已生成初始 admin API Key（仅显示这一次，请立即保存）",
 "api_key":"oak_ppvK-CZsptOgJdQuiGs1KGlThQkXbRGtUTnmaLowHz0"}
```

把它填到 `backend/.env` 的 `BOOTSTRAP_ADMIN_API_KEY`（或直接贴到前端界面），之后所有请求都要带凭据：

```bash
curl -H "Authorization: Bearer $API_KEY" localhost:8000/api/v1/agents
# 或：-H "X-API-Key: $API_KEY"
```

角色与权限（矩阵写死在代码里，不通过 API 修改）：

| 角色 | 权限 |
|---|---|
| `viewer` | 只读：Agent / 会话 / 消息 / 工具 / 审计 |
| `developer` | 只读 + 创建 Agent、发消息、调用**已被放行**的工具 |
| `admin` | 全部，含注册工具、维护策略、管理密钥 |

- `/health`、`/docs`、`/openapi.json` 不需要认证。
- 密钥只存 SHA-256 摘要，明文仅在创建响应里出现一次。
- 系统不允许停用最后一个可用的 admin 密钥，避免把自己锁在门外。
- `AUTH_ENABLED=false` 等价于匿名管理员，仅供本地调试，绝不要带进部署。

```bash
# 创建一把只读密钥
curl -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"ci-viewer","role":"viewer"}'
```

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
AUTH='-H "Authorization: Bearer $API_KEY"'

# 1. 注册工具（内建实现只有 echo / current_time）
curl -X POST localhost:8000/api/v1/tools -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d '{
  "name": "echo",
  "description": "回显文本",
  "input_schema": {"type":"object","properties":{"text":{"type":"string"}},
                   "required":["text"],"additionalProperties":false},
  "permissions": ["read:text"]
}'

# 2. 此时调用会被拒绝（deny-by-default）
curl -X POST localhost:8000/api/v1/agents/<AGENT_ID>/tools/echo/invoke \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d '{"arguments":{"text":"hi"}}'
# => {"status":"denied","error_code":"policy_denied", ...}

# 3. 显式放行后再调用
curl -X POST localhost:8000/api/v1/policy-rules -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"<AGENT_ID>","tool_name":"echo","effect":"allow"}'
# => {"status":"succeeded","result":{"text":"hi"}, ...}
```

注意：注册工具与写策略需要 `admin`；`developer` 只能调用已被放行的工具。

要点：

- 策略裁决：priority 大者优先 → agent 专属优先于全局 → 同层 deny 优先于 allow。
- **注册 ≠ 有执行能力**：只有内建白名单（`echo`、`current_time`）能真正执行；
  `execution_type=executor` 一律返回 `executor_not_available`，等 V0.3 隔离执行器。
- 每次调用都落 `ToolInvocation` 与 `AuditLog`（审计只记参数键名，不记参数值）。
- 模型只能提出调用请求：Agent Loop 里只会把**已放行**的工具告诉模型，
  被拒绝的结果会连同原因回灌给模型，最多 `MAX_TOOL_ITERATIONS`（默认 4）轮。

接口与状态码见 [docs/api-contract.md](docs/api-contract.md)。

## 运行记录与事件流

每次发消息都会创建一条 `Run`（主键就是返回给客户端的 `run_id`），并追加事件：

```text
run.started → model.request → model.response → [tool.result …] → run.completed
```

```bash
curl -H "Authorization: Bearer $API_KEY" localhost:8000/api/v1/runs
curl -H "Authorization: Bearer $API_KEY" localhost:8000/api/v1/runs/<RUN_ID>
curl -H "Authorization: Bearer $API_KEY" localhost:8000/api/v1/runs/<RUN_ID>/events
```

Run 上记录了 `iterations`、`tool_calls`、`prompt_chars`、`answer_chars`、`status` 与错误码；
失败运行同样落库（`status=failed` + `run.failed` 事件），不会被静默丢弃。

## 流式输出（SSE）

```bash
curl -N -X POST localhost:8000/api/v1/conversations/<ID>/messages/stream \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"content":"你好"}'
```

事件序列：

```text
run.started → delta(delta…) → [tool.call → tool.result …] → completed
                                          ↘ error（建流之后出错）
```

- `delta` 是增量文本；`completed` 带上落库后的 `user_message` / `assistant_message`。
- 鉴权、配额、会话存在性都在**建流之前**校验，失败仍是正常 HTTP 错误码；
  开始推送后只能通过 `error` 事件报告。
- 前端用 `fetch` + `ReadableStream` 解析（`EventSource` 只支持 GET 且带不了 Authorization 头）。
- 服务端使用纯 ASGI 中间件：`BaseHTTPMiddleware` 对 `StreamingResponse` 是已知坑，
  我们直接绕开它。

## 评测与回归

数据集是 JSON，用例 = 一次「发消息 + 断言」，可在 CI 里跑：

```bash
cd backend
python -m app.evals --suite evals/baseline.json
python -m app.evals --suite evals/baseline.json --suite evals/tools.json --json report.json
```

```
[baseline] 3/3 通过，耗时 210ms
  PASS  简单问答  (118ms)
  PASS  中文回复不丢字  (40ms)
  PASS  回复完整落库  (39ms)
```

支持断言：`contains` / `not_contains` / `regex` / `run_status` / `min_tool_calls` /
`max_tool_calls` / `max_duration_ms`。用例里写 `mock_reply` 或 `mock_tool_call`
即使用确定性 provider，不依赖真实模型；不写则走配置里的模型（可用于评模型质量）。
退出码 0/1，可直接卡 CI。

## 限流与配额

| 限制 | 环境变量 | 默认 | 计数维度 |
|---|---|---|---|
| 请求频率 | `RATE_LIMIT_PER_MINUTE` | 120 | 每个 API Key 每分钟 |
| 运行次数 | `DAILY_RUN_QUOTA` | 200 | 每个 API Key 每 24 小时 |

超限返回 429，错误码分别是 `rate_limited` 与 `quota_exceeded`，并带 `Retry-After`。
设为 `0` 表示关闭。限流后端可选 `memory` 或 `redis`，见上文「数据库与限流后端」。

## 前端界面

左栏三个视图，按当前角色的权限自动禁用无权操作（服务端仍然会再次校验）：

- **对话**：Agent / 会话 / 消息。
- **工具**：注册工具、启停工具、按 Agent 维护 allow/deny 策略、查看最近审计。
- **运行**：运行列表 + 事件流详情。

打开 http://localhost:5173 会先要求输入 API Key（存在浏览器本地）。


## MCP 工具（V0.2）

MCP 服务器**只能由运维通过环境变量声明**，API 无法注册：

```bash
MCP_SERVERS='{"echo": {"command": "python", "args": ["path/to/server.py"]}}'
```

声明的服务器可以被同步进工具注册表：

```bash
curl -X POST localhost:8000/api/v1/mcp/servers/echo/sync -H "Authorization: Bearer $API_KEY"
# => {"server":"echo","registered":[{...}],"skipped":["broken_schema: ..."]}
```

同步进来的工具形如 `echo.echo`（`服务器名.远程工具名`），`execution_type=mcp`，
`config={"server":"echo","tool":"echo"}`。它们**仍然完整走治理链**：

```text
Tool Registry → JSON Schema → Policy → Audit → 执行（通过 stdio 调用 MCP 服务器）
```

所以 MCP 工具默认也是拒绝的，必须显式加 allow 规则。

### 为什么命令行只能放环境变量

MCP 本质是「借壳执行」：一条 `command` 就是一次任意进程启动。如果允许通过
`POST /tools` 写入 command，那么拿到 `write:tools` 的管理员就等于拿到任意代码执行。
所以数据库里只保存**服务器名**，真正的启动参数留在运维手里。

### 两个实现细节

- 每次调用起一个 MCP 进程、用完即关（进程池是后续优化）；客户端读超时故意比整体
  预算宽松，让外层超时稳定映射成 `tool_timeout` 而不是随机的读超时。
- MCP stdio 规定 UTF-8。Windows 上 Python 子进程默认 cp936，会让中文内容变成
  乱码字节导致解析失败——客户端会给出「非 UTF-8 数据」的明确错误。

## 技能（Skill）

Skill 是**可复用的指令包**，一个技能可以挂到多个 Agent 上：

```bash
# 1. 建技能（需要 admin）
curl -X POST localhost:8000/api/v1/skills -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d '{
  "name": "polite",
  "description": "礼貌应答",
  "instructions": "回答时先打招呼，并在结尾询问是否还需要帮助。"
}'

# 2. 挂到 Agent 上（引用名称，创建/更新时校验必须存在）
curl -X PATCH localhost:8000/api/v1/agents/<AGENT_ID> \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"skill_names": ["polite"]}'
```

运行时技能内容会被拼进 system prompt：

```text
<原始 system_prompt>

## Skill: polite
回答时先打招呼，并在结尾询问是否还需要帮助。
```

要点：

- 技能内容计入 `SKILL_MAX_CHARS`（默认 8000），超预算截断并留标记，
  不会把上下文预算吃光。
- **缺失或停用的技能会被跳过而不是报错**——技能改动不会让历史 Agent 直接不可用；
  实际生效的技能列表记录在 `run.started` 事件里，可回溯。
- 只有 admin 能写技能（`write:skills`）；开发者可以引用已有技能。
- Agent 按**名称**引用技能而不是外键：代价是重命名会断引用，因此写入时校验存在性。

## OIDC / JWT 认证（可选）

除了 API Key，还可以直接接企业 IdP。三项配齐即启用：

```bash
OIDC_ISSUER=https://idp.example.com
OIDC_AUDIENCE=agent-platform
OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json
```

之后请求可以直接带 IdP 签发的 access token：

```bash
curl -H "Authorization: Bearer $ACCESS_TOKEN" localhost:8000/api/v1/agents
```

- **与 API Key 并存**：凭据带两个点就按 JWT 处理，否则按 API Key 查库
  （我们的 Key 形如 `oak_xxx`，不含点，两者不会混淆）。
- 只信任 IdP 的 JWKS，本地不存公钥；JWKS 有 TTL 缓存，遇到未知 `kid`
  会**强制刷新一次**再判定失败——IdP 轮换密钥不需要重启服务。
- 校验 `signature`、`iss`、`aud`、`exp`、`nbf`，`sub/exp/iss/aud` 为必需字段。
- 角色来自 `OIDC_ROLE_CLAIM`（默认 `role`），只接受 `admin`/`developer`/`viewer`；
  取不到或不认识的值**降级为** `OIDC_DEFAULT_ROLE`（默认 `viewer`），
  避免 IdP 加了个新角色就把所有人挡在门外。
- `GET /api/v1/me` 会返回 `auth_method`（`api_key` / `oidc` / `none`），便于排查。

> 目前只做了**认证**：`sub` 只用于审计和 Run 归属，还没有组织/租户隔离。
> 也就是说所有通过认证的身份看到的是同一份数据。

## 多租户隔离

所有业务表都带 `tenant_id`，请求的租户由凭据决定：

| 凭据 | 租户来源 |
|---|---|
| API Key | 密钥自身的 `tenant_id` |
| OIDC / JWT | `OIDC_TENANT_CLAIM`（默认 `tenant`）claim，取不到则 `default` |
| 认证关闭（本地） | `default` |

**隔离不是靠每条查询自觉加 `where`**，而是在数据访问层统一施加：

```python
# app/db/tenancy.py
with_loader_criteria(Base, _criteria, include_aliases=True)
```

这个 ORM 事件会被注入到所有 SELECT，实测覆盖 `select()`、`Session.get()`、
`count()` 以及带显式 where 的查询。因此跨租户访问**统一表现为 404**，
不会泄露「这个 id 是否存在」。

确实需要跨租户读的地方必须显式声明，让绕过在代码里可见：

```python
select(ApiKey).execution_options(include_all_tenants=True)  # 仅认证时按哈希查密钥
```

### 开一个新租户

admin 可以为指定租户签发密钥：

```bash
curl -X POST localhost:8000/api/v1/api-keys -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"acme-admin","role":"admin","tenant_id":"acme"}'
```

之后用这把密钥创建的资源都落在 `acme` 租户下，对其他租户不可见。

### 已知取舍

- 工具与技能的名称是**租户内唯一**（`(tenant_id, name)`），因此不同租户可以各自
  拥有 `echo`。
- API Key 的名称仍是**全局唯一**（它是人类标签，改动涉及不可回滚的约束重建），
  跨租户重名会返回 409。
- 没有组织/成员/计费概念，也没有跨租户的管理员视图。

## 数据库与限流后端

### SQLite → PostgreSQL

`DATABASE_URL` 换成 PG 即可，schema 由同一套 Alembic 迁移负责：

```bash
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/agent_platform
alembic upgrade head
```

驱动的方言差异（`TIMESTAMP WITH TIME ZONE`、索引语法等）由 Alembic 处理；
`render_as_batch` 只对 SQLite 开启。`tests/test_postgres_dialect.py` 会用离线模式
（`alembic upgrade head --sql`）校验迁移能为 PG 渲染出合法 SQL——不需要真实 PG 实例。

### 限流后端

| `RATE_LIMIT_BACKEND` | 说明 |
|---|---|
| `memory`（默认） | 进程内滑动窗口，零依赖，单实例够用 |
| `redis` | ZSET 滑动窗口，多副本共享计数；需 `REDIS_URL` |

```bash
RATE_LIMIT_BACKEND=redis
REDIS_URL=redis://localhost:6379/0
```

两种后端语义一致（滑动窗口、429 + `Retry-After`）。Redis 实现用
`pipeline(transaction=True)` 把「清理过期 + 记账 + 计数 + 续期」做成原子操作，
不会在窗口边界出现双倍突发。测试用 `fakeredis` 覆盖，无需真实 Redis。

每日运行配额始终基于数据库中的 Run 记录统计，与限流后端无关。

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
- API Key 认证 + admin/developer/viewer 三角色 RBAC
- OIDC / JWT 认证（与 API Key 并存，JWKS 验签）
- 多租户隔离（数据层强制过滤，跨租户 404）
- Run / RunEvent 持久化与事件流查询
- 每密钥限流与每日运行配额（可切换 Redis 后端）
- PostgreSQL / SQLite 双支持（同一套迁移）
- SSE 流式输出（前端增量渲染）
- 评测与回归（JSON 数据集 + 断言 + CLI）
- MCP 工具接入（stdio 客户端 + 同步 + 仍走治理链）
- 技能系统（可复用指令包 + 运行时注入）
- React 界面：对话、工具与策略、运行与审计三个视图

## 重要边界
V0.2 不执行任意 Shell / Python / Node 代码。已有 API Key 认证与三角色 RBAC，
但**没有**多租户隔离、OIDC/SSO、密钥轮换与过期、限流与配额、生产级任务恢复。
Rust Executor 目前是架构预留，不是可用 Sandbox。
生产环境必须补充隔离机制、组织级鉴权、限流和密钥管理。

# Enterprise Agent Platform

V0.1 起步，当前已到 V0.2 Harness + V0.3 执行器竖切面：React + TypeScript 前端、Python + FastAPI Agent Core、Rust Executor（Docker 隔离执行）。

## 目录结构

```text
backend/          FastAPI + SQLAlchemy + Alembic（Agent Core）
frontend/         React + TypeScript（Vite）
rust-executor/    Rust Executor —— Docker 隔离执行后端（V0.3）
scripts/          一键启动脚本（dev.py / dev.ps1 / dev.sh）
docs/             PRD / 架构 / 数据模型 / API 契约 / 安全基线 / Roadmap
```

## 一键启动（推荐）

```powershell
# Windows
.\scripts\dev.ps1

# macOS / Linux
sh scripts/dev.sh
```

> PowerShell 若提示禁止运行脚本：`powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1`；
> macOS/Linux 想直接 `./scripts/dev.sh` 的话先 `chmod +x scripts/dev.sh`。

首次运行会自动创建 `backend/.venv`、安装后端依赖与前端依赖（`npm ci`），
然后同时启动后端和前端，等两边就绪后打印地址；首次生成的 admin key 会被单独高亮一次。
日志带 `[backend]` / `[frontend]` 前缀，按 `Ctrl+C` 一起停止。

常用参数：

| 参数 | 作用 |
|---|---|
| `--executor` | 构建 release 执行器并自动设置 `EXECUTOR_ENABLED` / `EXECUTOR_COMMAND`（需要 Docker） |
| `--backend-port 8000` / `--frontend-port 5173` | 换端口（会同步设置 CORS 与前端 API 地址） |
| `--backend-only` / `--frontend-only` | 只启动一侧 |
| `--no-setup` | 不自动创建 venv / 安装依赖 |
| `--install` | 强制重装依赖 |
| `--python PATH` | 使用现成解释器，跳过 venv |

等价命令是 `python scripts/dev.py`。启动前会检查端口占用，避免健康检查误命中旧进程；
`--help` 可查看全部参数。

## 手动启动（分步）

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

`CONTEXT_MAX_CHARS`（默认 24000）限制单次请求的上下文预算。

**历史装不下时会先摘要，而不是直接丢弃。** 触发条件：历史长度超过预算的
`CONTEXT_SUMMARY_TRIGGER_RATIO`（默认 0.8），且尚未被摘要覆盖的消息达到
`CONTEXT_SUMMARY_KEEP_MESSAGES`（默认 6）条。摘要写回会话（`summary` +
`summary_message_count`），下一轮只压缩新增部分，不会重复调用模型。最近 6 条消息
始终原样保留；摘要失败则退回"整条丢弃"，不影响对话本身。

摘要之后的兜底仍按原逻辑：从最旧的消息开始丢弃，最新的用户输入始终保留
（过长则截断尾部），并在日志中记录 `context trimmed to budget` 事件。

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
- **注册 ≠ 有执行能力**：内建白名单（`echo`、`current_time`）可直接执行；
  `executor` 需要 Docker 与执行器（默认关闭），`mcp` 需要运维声明服务器。
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

### 断线续传

**客户端断开不会杀死运行**：运行跑在独立任务里，断开只是退订；它会继续跑完并落库。

重连时用回放端点按游标续订：

```bash
curl -N -H "Authorization: Bearer $API_KEY" \
  "localhost:8000/api/v1/runs/<RUN_ID>/events/stream?after=0"
# 也支持标准头：-H "Last-Event-ID: 5"
```

- 每条事件都带 `id: <seq>`，回放从 `after` 之后开始；
- 运行仍在进行时会持续跟随，直到终态或 `RUN_STREAM_MAX_SECONDS`；
- 结束时发一条 `stream.end`（带 `status`），前端据此判断成败。

**能力边界**：增量文本（`delta`）不落库，所以重连能拿到完整生命周期事件与
**最终消息**，但拿不回断流前已经显示的那段文字。前端断流后会提示"正在恢复"，
并用回放到的 `completed` 事件替换临时草稿。

SQLite 下需要 WAL（已在连接时自动开启），否则运行任务写、回放端点读会互相阻塞。

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

左栏四个视图，按当前角色的权限自动禁用无权操作（服务端仍然会再次校验）：

- **对话**：Agent / 会话 / 消息（SSE 增量渲染）。
- **工具**：注册 builtin / executor 工具（含运行时与镜像）、启停工具、按 Agent 维护 allow/deny 策略、查看最近审计。
- **运行**：运行列表 + 事件流详情。
- **组织**：当前租户信息、开通租户、成员增删改与角色调整。

打开 http://localhost:5173 会先要求输入 API Key（存在浏览器本地）。
界面里没有租户切换：租户由你输入的密钥决定。

细节处理：

- 失败操作以浮动提示呈现（带 `request_id`，便于对照后端日志），可手动关闭。
- 列表在首次加载时显示「加载中…」，避免把「还没加载」误读成「没有数据」。
- 窄屏（≤1024px）侧栏变成顶部条、双栏变单栏；≤640px 进一步隐藏次要信息，
  表格改为横向滚动，不撑破布局。

界面截图（由 `frontend/e2e/smoke.py` 在 headless 浏览器里实际跑出来后截取）：

| 对话 | 工具与策略 |
|---|---|
| ![对话](docs/screenshots/chat.png) | ![工具](docs/screenshots/tools.png) |

| 组织与成员 | 手机宽度（420px） |
|---|---|
| ![组织](docs/screenshots/organization.png) | ![移动端](docs/screenshots/mobile.png) |

### 浏览器冒烟测试

`frontend/e2e/smoke.py` 用 Playwright 驱动真实浏览器，覆盖准入、新建 Agent/会话、
流式发消息、四视图切换、窄屏布局与错误提示。用法见
[frontend/e2e/README.md](frontend/e2e/README.md)。


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

- **连接池**：按服务器复用 MCP 子进程，避免每次调用都付一次启动 + 握手。
  同一服务器上的调用**串行化**（stdio JSON-RPC 是有状态单通道，并发会串响应）；
  连接空闲 `MCP_IDLE_SECONDS`（默认 60）后回收，进程死亡或协议出错立即丢弃重建，
  应用退出时统一清理。
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

## 组织与成员

租户（组织）现在是**有生命周期、有成员**的实体，而不再只是一个字符串：

```bash
# 1. 开通租户（admin）
curl -X POST localhost:8000/api/v1/tenants -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d '{"id":"acme","name":"Acme Inc"}'

# 2. 为该租户签发第一把密钥（admin）
curl -X POST localhost:8000/api/v1/api-keys -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"acme-admin","role":"admin","tenant_id":"acme"}'

# 3. 把 OIDC 用户加进租户并指定角色
curl -X POST localhost:8000/api/v1/memberships -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d '{"subject":"oidc:user-1","role":"developer"}'
```

### 成员关系会覆盖 IdP 的角色

OIDC 用户的角色来自 token 的 claim，但**如果他是某个租户的成员，则以成员关系为准**：

```text
token.role = admin   +   membership.role = viewer   →   实际权限 = viewer
```

这样平台可以在不改 IdP 的前提下按租户调整授权。不设成员时沿用 claim 角色；
把 `OIDC_REQUIRE_MEMBERSHIP=true` 打开后，**没有成员的 OIDC 用户会被直接拒绝**（403），
适合"必须显式邀请才能进入"的场景。

### 其它行为

- `GET /tenants/current` 返回当前租户；`GET/POST/PATCH/DELETE /memberships` 管理成员，
  全部只在**自己所在租户**内生效。
- 为不存在的租户发密钥会返回 404——必须先开通租户。
- 租户被停用（`status=disabled`）后，该租户的所有请求立即 403。
- 已知取舍：**任何 admin 都能开通新租户**（没有平台级超管角色）。
  自托管场景可接受，但在多组织托管场景需要额外限制。

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

## 可观测性（OpenTelemetry）

默认关闭，埋点代码始终存在——没有 provider 时 OTel API 就是 no-op，成本接近于零。
需要时配三项即可：

```bash
OTEL_ENABLED=true
OTEL_SERVICE_NAME=agent-platform
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # Collector / Jaeger 的 OTLP/HTTP
OTEL_SAMPLE_RATIO=1.0                                # 0..1，生产建议调低
```

### 链路

```text
http.server
└─ agent.run            (tenant.id, agent.id, run.id, model, run.status)
   ├─ model.request     (iteration, reply.text_chars, reply.tool_calls)
   ├─ tool.call         (tool.name, tool.status, tool.allowed)
   │  └─ mcp.call       (mcp.server, mcp.tool, mcp.is_error)
   └─ model.request
```

`run_id` / `request_id` 都在 span 属性里，能和持久化的 `Run` / `RunEvent` 对上。
流式与非流式走同一套埋点（`agent.stream=true` 区分）。

### 指标

只有三个，刻意保持低基数（**不把工具名放进指标属性**，否则会随租户增长）：

- `agent.runs`（按 `status`）
- `agent.run.duration_ms`（按 `status`）
- `tool.invocations`（按 `status`）

### 日志关联

有活动 span 时，JSON 日志里会自动带上 `trace_id` / `span_id`，可以直接从日志跳到链路：

```json
{"level":"INFO","logger":"app.services.agent_runtime","message":"agent run started",
 "request_id":"req_...","run_id":"run_...","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736"}
```

### 本地验证

不需要部署 Jaeger：测试用 `InMemorySpanExporter` / `InMemoryMetricReader` 断言整棵
span 树、父子关系、属性与指标（`tests/test_telemetry.py`）。
真实导出（Collector / Jaeger）需要你启动一个 OTLP 后端——本机 Docker 未启动，未实测。

## 审计防篡改

审计日志是**只追加**的，并且每条记录都链进按租户的哈希链：

```text
entry_hash = sha256(prev_hash + canonical(entry))
canonical   = 固定字段 + 固定顺序的 JSON
```

```bash
curl -H "Authorization: Bearer $API_KEY" localhost:8000/api/v1/audit-logs/verify
# => {"valid":true,"checked":12,"legacy":0,"broken_at":null,"reason":null}
```

被改动时会指出首个断裂点：

```json
{"valid":false,"checked":1,"broken_at":2,"reason":"entry_hash 不匹配（内容被修改）"}
```

能被发现的改动：

| 行为 | 检测方式 |
|---|---|
| 改某条记录的内容 | `entry_hash` 不匹配 |
| 删除中间某条 | `seq` 不连续 + `prev_hash` 断链 |
| 插入伪造记录 | `seq` 不连续 |
| 通过 ORM 更新/删除 | 直接抛出 `RuntimeError`（只追加保护） |

### 边界：这是「防篡改」不是「防重写」

- **删除尾部记录检测不到**——剩下的链本身仍然自洽（`checked` 会变小，但 `valid` 仍为 true）。
- 拥有数据库完整写权限的人可以重算整条链，让它重新自洽。

真正的防篡改需要**外部锚定**：定期把链头（最新 `entry_hash`）写入不可变介质
（WORM 存储、对象锁、外部透明日志），或者把链头随日志导出到独立系统。
当前实现只能保证"局部改动一定会被发现"。

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

## 隔离执行器（V0.3 竖切面）

`execution_type=executor` 的工具现在会真正进入 **Docker 容器**执行。链路：

```text
Tool Registry → JSON Schema → Policy → Audit → Rust Executor → Docker
                                                      └─ /workspace/output → 产物
```

### 构建与启用

```bash
cargo build --release --manifest-path rust-executor/Cargo.toml
export EXECUTOR_ENABLED=true
export EXECUTOR_COMMAND="$PWD/rust-executor/target/release/agent-executor"
# 可选：EXECUTOR_IMAGE=python:3.12-slim
```

宿主机必须能访问 Docker daemon。`agent-executor --check` 可以在不执行任何东西的
前提下检查 Docker 是否就绪，并打印默认镜像与容器用户。

> 不要把 `/var/run/docker.sock` 挂进业务容器再运行执行器：那会让执行器所在容器
> 获得宿主机 root 等价能力。执行器应运行在受控宿主机或专用 runner 上。

### 注册与调用

```bash
# 1. 注册（命令只能来自调用参数，config 里不允许写死命令）
curl -X POST localhost:8000/api/v1/tools -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d '{
  "name": "run_python",
  "description": "执行一段 Python 代码",
  "execution_type": "executor",
  "config": {"runtime": "python", "image": "python:3.12-slim"},
  "input_schema": {"type":"object","properties":{"command":{"type":"string"}},
                   "required":["command"],"additionalProperties":false}
}'

# 2. 默认拒绝，仍需显式放行
curl -X POST localhost:8000/api/v1/policy-rules -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"<AGENT_ID>","tool_name":"run_python","effect":"allow"}'

# 3. 调用
curl -X POST localhost:8000/api/v1/agents/<AGENT_ID>/tools/run_python/invoke \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"arguments":{"command":"print(1+1)"}}'
```

成功时 `result`：

```json
{"exit_code": 0, "stdout": "2\n", "stderr": "", "stdout_truncated": false,
 "duration_ms": 412,
 "artifacts": [{"path": "report.txt", "size": 14, "sha256": "...", "artifact_id": "..."}]}
```

失败时 `status=failed`，`error_code` 是 `tool_execution_failed` / `tool_timeout` /
`executor_not_available`，`error_message` 会带上截断后的 stderr，方便模型自我修正。

### 产物

命令写到 `/workspace/output/` 下的普通文件会被收集、落盘并登记：

```bash
GET /api/v1/tool-invocations/{invocation_id}/artifacts
GET /api/v1/artifacts/{artifact_id}/download
```

- 只收集 `output/` 下的普通文件；**不跟随符号链接**，`../` 与绝对路径一律拒绝；
- 大小以宿主机重新读取为准，SHA-256 也在 Python 侧重算，执行器上报的清单只当索引；
- 落盘路径 `ARTIFACT_DIR/<tenant>/<invocation_id>/<相对路径>`，下载强制
  `application/octet-stream` + attachment，避免把用户内容当 HTML 渲染。

### 隔离参数

| 参数 | 值 | 作用 |
|---|---|---|
| `--network none` | 固定 | 默认禁网 |
| `--read-only` | 固定 | 根文件系统只读 |
| `--tmpfs /tmp` | 固定 | 唯一可写临时区 |
| `--cap-drop ALL` | 固定 | 去掉所有 capability |
| `--security-opt no-new-privileges` | 固定 | 禁止提权 |
| `--pids-limit` | `EXECUTOR_PIDS_LIMIT` | 进程数上限 |
| `--memory` / `--memory-swap` | `EXECUTOR_MEMORY_MB` | 内存上限（不落 swap） |
| `--cpus` | `EXECUTOR_CPUS` | CPU 上限 |
| `--user` | `EXECUTOR_USER` | 默认用后端进程的 uid:gid（Windows 为 0:0） |
| `-v <workspace>:/workspace` | 每次调用 | 只挂载本次工作目录，不挂 docker socket |

Windows 上 Docker Desktop 的 uid 映射不可靠，容器默认以 root 运行；在
`--read-only` + `--cap-drop ALL` + `--network none` + 资源限制之下仍有边界，
但**不要**把它当成与 Linux 非 root 同等级别的隔离。

### 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `EXECUTOR_ENABLED` | `false` | 总开关；关闭时执行器工具返回 `executor_not_available` |
| `EXECUTOR_COMMAND` | `agent-executor` | 可执行文件，允许带参数（POSIX 引号规则） |
| `EXECUTOR_IMAGE` | `python:3.12-slim` | 默认镜像 |
| `EXECUTOR_USER` | 后端进程 uid:gid | 容器内用户；Windows 为 0:0 |
| `EXECUTOR_TIMEOUT_SECONDS` | `30` | 硬超时；工具自身 timeout 只能更小 |
| `EXECUTOR_MEMORY_MB` / `EXECUTOR_CPUS` | `512` / `1.0` | 资源上限 |
| `EXECUTOR_PIDS_LIMIT` | `128` | 进程数上限 |
| `EXECUTOR_MAX_OUTPUT_BYTES` | `65536` | stdout/stderr 各自上限，超出截断并标记 |
| `EXECUTOR_MAX_ARTIFACT_BYTES` | `5242880` | 单文件上限（0 表示不限） |
| `EXECUTOR_MAX_ARTIFACTS` | `20` | 单次调用产物数上限 |
| `EXECUTOR_MAX_TOTAL_ARTIFACT_BYTES` | `20971520` | 单次调用落盘总量上限 |
| `EXECUTOR_MAX_CONCURRENCY` | `2` | 同时运行的容器数 |
| `EXECUTOR_GRACE_SECONDS` | `10` | 工具超时后给执行器收尾的宽限 |
| `ARTIFACT_DIR` | `./artifacts` | 产物根目录 |
| `EXECUTOR_WORKDIR` | 系统临时目录 | 执行工作目录的父目录 |

### 已知边界

- 只有 Docker 后端；gVisor / Firecracker 未接入。
- 输入文件、网络白名单、镜像预热/快照、执行中流式日志都还没有。
- 单机同步调用：没有队列与跨进程调度；后端进程崩溃时容器可能残留，
  可用 `docker ps --filter label=agent-executor=1` 清理。
- 产物没有保留期与清理任务：`ARTIFACT_DIR` 会随调用增长，需要运维自行制定清理策略；
  删除 `tool_artifacts` 记录不会删除磁盘文件。
- 前端对话里暂不展示工具调用结果与产物，产物需要通过上面的 API 查看。
- 人工审批、按工具调用配额仍未实现。

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
- 组织与成员（租户生命周期、成员关系覆盖 IdP 角色）
- 审计哈希链（只追加 + 篡改可检出）
- 上下文摘要、长期记忆、子代理（派生运行）
- OpenTelemetry 链路 / 指标 / 日志关联（默认关闭，可接 OTLP）
- Run / RunEvent 持久化与事件流查询
- 每密钥限流与每日运行配额（可切换 Redis 后端）
- PostgreSQL / SQLite 双支持（同一套迁移）
- SSE 流式输出（前端增量渲染）
- 评测与回归（JSON 数据集 + 断言 + CLI）
- MCP 工具接入（stdio 客户端 + 同步 + 仍走治理链）
- 技能系统（可复用指令包 + 运行时注入）
- 隔离执行器（Docker：超时取消、CPU/内存/PID 限制、输出截断、产物收集）
- 工具产物索引、SHA-256 校验与下载接口
- React 界面：对话、工具与策略、运行与审计、组织与成员四个视图

## 重要边界

- **代码执行只走 Docker 后端，且默认关闭**。容器共享宿主机内核，它不是
  gVisor / Firecracker 级别的安全边界；只应运行可信度可控的代码。更强的隔离未接入。
- 没有人工审批、按工具的调用配额、输入文件与网络白名单。
- 没有 Temporal 级的持久任务恢复：运行与容器都绑在当前进程上，进程崩溃会留下
  半截运行与可能残留的容器（手工清理见上文）。
- 审计哈希链只保证局部改动可发现；删除链尾或整体重算需要外部锚定（WORM/透明日志）。
- 没有平台级超管、计费与成本计量；API Key 轮换/过期未实现。
- 多租户隔离、OIDC、限流与配额已实现（见上文），但跨租户管理视图仍缺。
- 生产环境仍需补充：组织级鉴权细化、成本控制、任务恢复与更强的执行隔离。

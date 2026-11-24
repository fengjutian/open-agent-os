# API 契约 V0.1

Base URL：`/api/v1`

## 通用约定

### 认证

除 `/health`、`/docs`、`/openapi.json` 外，所有接口都需要凭据：

```http
Authorization: Bearer oak_xxxxxxxx
```

或 `X-API-Key: oak_xxxxxxxx`。**如果 Bearer 的值含两个点，会被当作 JWT 校验**
（需服务端配置 `OIDC_ISSUER` / `OIDC_AUDIENCE` / `OIDC_JWKS_URL`）。

缺失或无效返回 401 `unauthorized`，权限不足返回 403 `forbidden`。
`GET /me` 的 `auth_method` 会告诉你这次请求用的是哪种凭据。

角色权限矩阵（全局角色，不支持资源级授权）：

| 角色 | 权限点 |
|---|---|
| `viewer` | `read` |
| `developer` | `read`、`write:agents`、`invoke:tools` |
| `admin` | 全部，含 `write:tools`、`write:policy`、`write:keys` |

### 组织与成员

- `POST /tenants`：开通租户，body `{ id, name }`（`id` 形如 `acme`，创建后不可改）。
  重名 409，命名不合法 422。需要 `write:members`（仅 admin）。
- `GET /tenants/current`：当前租户。
- `GET /memberships`：当前租户的成员列表（分页）。
- `POST /memberships`：`{ subject, role }`，`subject` 与 `Principal.key_id` 对齐
  （OIDC 用户形如 `oidc:user-1`）；重复 409。
- `PATCH /memberships/{id}`：`{ role }`。
- `DELETE /memberships/{id}`：移出租户，204。

成员的 `role` 会**覆盖**凭据里的角色；`OIDC_REQUIRE_MEMBERSHIP=true` 时，
没有成员的 OIDC 用户一律 403。为不存在的租户签发密钥返回 404；
租户 `status=disabled` 后该租户的所有请求 403。

### 知识库

- `POST /knowledge-bases`：建库，body `{ name, description? }`。
- `GET /knowledge-bases`：列表，含 `chunk_count` 与 `embedding_model`。
- `DELETE /knowledge-bases/{id}`：删库，连带文档与块，204。
- `POST /knowledge-bases/{id}/documents`：导入纯文本文档，body `{ name, content }`；
  同步切块并向量化，返回 `chunk_count`。
- `GET /knowledge-bases/{id}/documents`：文档列表。
- `DELETE /knowledge-bases/{id}/documents/{doc_id}`：删文档并清块，204。
- `POST /knowledge-bases/{id}/search`：检索，body `{ query, limit? }`，
  返回 `{ hits: [{ document, seq, score, text }], count }`。

检索默认按**词面覆盖率**排序（`KNOWLEDGE_RANKING=vector` 可切余弦），
且要求查询与块至少共享一个词面特征——本地哈希向量在短文本上的随机相似度足以
把无关内容排进结果，门控是必需的。模型侧通过内置工具 `knowledge_search` 使用。

### 用量报表

- `GET /usage/overview?days=30`：窗口内的总量指标——运行数、成功/失败、工具调用、
  输入输出字符数、平均耗时、活跃 Agent/模型/工具数、日配额。`days` 取值 1..366。
- `GET /usage/timeline?days=30&bucket=day`：按时间桶的序列，`bucket` 为
  `day` / `week` / `month`；**没有数据的时间桶也会出现**（补零），便于直接画折线。
- `GET /usage/breakdown?days=30&dimension=agent`：按维度分解，
  `dimension` 为 `agent` / `model` / `tool`，按用量降序返回。

口径：统计的是**运行次数与字符数**，不是 token——模型网关不解析 `usage`，
所以这个接口回答不了「花了多少钱」。各维度的字段含义略有差别：
`agent` / `model` 的运行数与字符数都来自运行记录；`tool` 维度的「运行」是
用过该工具的运行数，「工具调用」是该工具的调用次数，字符数不适用。

### 长期记忆

- `POST /memories`：写入一条记忆，body `{ content, agent_id?, tags?, source_conversation_id? }`；
  `agent_id` 为空表示租户级（所有 Agent 可见）。
- `GET /memories?q=&agent_id=`：列表，`q` 是包含匹配。
- `DELETE /memories/{id}`：删除，204。
- `POST /conversations/{id}/memories/distill`：把该会话交给模型提炼 0-N 条记忆并落库；
  模型返回非 JSON 时返回空数组而不是报错。

运行时按**词法相关度**自动注入相关记忆（最多 `MEMORY_INJECT_LIMIT` 条），
注入位置是 system prompt 末尾的 `[相关记忆]` 块；检索异常一律降级为"没有记忆"。
### 人工审批（HITL）

标了 `requires_approval` 的工具在执行前会停：

- `POST /conversations/{id}/messages` 返回 **202** `{run_id, approval_id, tool, status}`；
  流式端点则发出 `run.waiting_approval` 事件后结束本次流。
- `GET /approvals?status=pending`：待审批列表（含工具名与参数）。
- `POST /approvals/{id}/approve`：执行工具并从检查点续跑同一个 run，
  返回最终消息；若同批还有工具需要审批，再返回 202。
- `POST /approvals/{id}/reject`：不执行，把拒绝原因作为工具结果回灌，模型继续收尾。

已处理的审批再次决定返回 **409**；审批结论写入审计（`tool.approval.approved` /
`tool.approval.rejected`），参数只记键名。
### 定时任务

- `POST /agents/{agent_id}/cron`：创建，body `{ name, prompt, schedule, conversation_id?, enabled? }`。
  `schedule` 是标准 5 段 cron（分 时 日 月 周，**UTC**），也支持 `@daily` 等别名；
  非法表达式返回 **422**（而不是 500）。
- `GET /agents/{agent_id}/cron`：列表（分页）。
- `GET /cron/{job_id}`：详情，含 `next_run_at` / `last_run_at` / `last_status` / `last_error`。
- `PATCH /cron/{job_id}`：改名称/提示词/表达式/启用状态；改表达式会重算 `next_run_at`。
- `DELETE /cron/{job_id}`：删除，204。
- `POST /cron/{job_id}/run-now`：立即执行一次，不改动排期，返回更新后的任务。

调度由服务端进程内循环驱动（`CRON_ENABLED` 控制，默认每 `CRON_TICK_SECONDS` 秒一轮）。
**多实例部署会重复执行**，需要外部分布式锁；这条限制写在 `docs/features/cron.md`。
### 租户

所有资源都属于某个租户。租户由凭据决定（API Key 的归属 / OIDC 的 `tenant` claim），
**不是**请求参数——客户端无法通过改参数切换租户。

- 跨租户访问一律返回 **404**（不区分「不存在」与「不属于你」）。
- 列表、审计、运行记录都只返回本租户数据。
- `POST /api-keys` 可带 `tenant_id`，用于为该租户签发密钥（仅 admin）。

### 分页

所有列表接口统一返回分页信封：

```json
{ "items": [], "total": 0, "limit": 50, "offset": 0 }
```

查询参数：

| 参数 | 默认 | 约束 |
|---|---|---|
| `limit` | 50 | 1–200，越界返回 422 |
| `offset` | 0 | ≥ 0 |

### 错误

所有错误统一返回：

```json
{
  "error": {
    "code": "not_found",
    "message": "Agent not found",
    "request_id": "req_0ce0bda1701c4e4a8bcdb0db96a7b0ce",
    "details": null
  }
}
```

- `code` 稳定，客户端应按它分支；`message` 仅供展示。
- `request_id` 与响应头 `X-Request-ID` 一致；客户端也可主动传入该请求头以串联链路。
- 未预期异常只返回 `internal_error`，不泄露堆栈或上游响应体。

| code | HTTP | 含义 |
|---|---|---|
| `unauthorized` | 401 | 缺少或无效的 API Key |
| `forbidden` | 403 | 当前角色权限不足 |
| `not_found` | 404 | 资源不存在 |
| `validation_error` | 422 | 请求体或查询参数校验失败（`details` 为字段级错误） |
| `conflict` | 409 | 资源冲突（工具名重复、停用最后一个 admin 密钥等） |
| `rate_limited` | 429 | 请求过于频繁（带 `Retry-After`） |
| `quota_exceeded` | 429 | 24 小时内运行次数超限 |
| `model_not_configured` | 424 | 缺少模型密钥等配置 |
| `model_provider_error` | 502 / 504 | 模型服务错误或超时 |
| `internal_error` | 500 | 未预期异常 |

工具治理相关（出现在调用响应的 `error_code` 字段，见下）：

| code | 含义 |
|---|---|
| `tool_not_found` | 工具未注册 |
| `tool_disabled` | 工具已停用 |
| `tool_arguments_invalid` | 参数不符合工具的 input_schema |
| `policy_denied` | 服务端策略拒绝 |
| `tool_timeout` | 工具执行超时 |
| `tool_execution_failed` | 工具执行失败（含内建实现缺失） |
| `executor_not_available` | 需要 V0.3 隔离执行器，当前不可用 |

## 接口

- `POST /agents`：创建 Agent，body 包含 name、description、system_prompt、model。
  省略 `model` 时取 `MODEL_NAME` 配置。
- `GET /agents`：Agent 列表（分页）。
- `GET /agents/{agent_id}`：Agent 详情。
- `PATCH /agents/{agent_id}`：更新 Agent。
  未传的字段保持不变；`name`/`system_prompt`/`model` **不可显式传 null**（返回 422），
  `description` 可传 null 以清空。
- `DELETE /agents/{agent_id}`：删除 Agent，级联删除其会话与消息，返回 204。
- `POST /agents/{agent_id}/conversations`：创建会话，body `{ "title": "New conversation", "mode": "craft" }`。
  `mode` 可选，取值 `ask` / `plan` / `craft`，缺省用服务端默认值。
- `GET /agents/{agent_id}/conversations`：会话列表（分页），按最近活动倒序，含 `mode`。
- `PATCH /conversations/{conversation_id}`：改标题或模式，body `{ "title"?, "mode"? }`；
  显式传 `null` 返回 422。
- `GET /conversations/{conversation_id}/messages`：消息历史（分页），按时间正序。
- `POST /conversations/{conversation_id}/messages`：发送消息，body `{ "content": "...", "mode": "ask" }`。
  `mode` 是**本轮**覆盖，不改写会话上的模式。

### 会话模式

| 模式 | 模型可见工具 | 行为 |
|---|---|---|
| `craft`（默认） | 全部（仍受策略约束） | 正常执行 |
| `ask` | 仅只读（内建 `current_time` + 标记了 `read_only` 的工具） | 只回答问题，不改状态 |
| `plan` | 同上 | 先产出分步计划，停下等确认 |

未知取值一律按服务端默认值（`craft`）处理；被模式裁掉的工具模型**根本看不到**，
因此不会发起调用，也不需要额外的拒绝路径。
  响应：

```json
{
  "run_id": "run_d392dd62d4ae42cf8b69bc39e6045b82",
  "user_message": { "...": "" },
  "assistant_message": { "...": "" }
}
```

  `run_id` 标识本次 Agent 运行，与后端日志中的 `run_id` 字段对应，便于逐次排查。
  模型调用失败时整次运行回滚，不会留下半截的用户消息。
- `GET /health`：健康检查。

## 时间字段

所有时间均为 UTC，序列化为带 `Z` 后缀的 ISO 8601，例如 `2026-11-22T12:37:54.123456Z`。
（SQLite 不保存时区，服务端会在输出时补上 UTC 标记。）

## V0.2 工具治理

### 工具注册表

- `POST /tools`：注册工具。body：`name`（`^[a-z][a-z0-9_.-]{1,63}$`）、`description`、
  `input_schema`（JSON Schema）、`permissions`、`timeout_seconds`（0–60）、
  `execution_type`（`builtin` | `mcp` | `executor`）、`enabled`。重名返回 409，
  `input_schema` 非法返回 422。
- `GET /tools`：列表（分页）。
- `GET /tools/{tool_id}`：详情。
- `PATCH /tools/{tool_id}`：更新（`description`/`permissions`/`timeout_seconds`/`enabled`）。

**注册工具不会产生执行能力。** `builtin` 只执行平台白名单里的实现；`mcp` 只能引用
`MCP_SERVERS` 里由运维声明的服务器；`executor` 交给 Docker 执行器（默认关闭），
且命令必须来自调用参数。

### 策略规则

- `POST /policy-rules`：`agent_id`（为空表示全局）、`tool_name`（或 `*`）、
  `effect`（`allow` | `deny`）、`priority`（默认 100）、`note`。
- `GET /policy-rules`：列表，可按 `agent_id` 过滤。

裁决顺序：**priority 大者优先 → agent 专属优先于全局 → 同层 deny 优先于 allow**；
没有任何 allow 命中即拒绝（deny-by-default）。

### 受控调用

`POST /agents/{agent_id}/tools/{tool_name}/invoke`

```json
{ "arguments": { "text": "hello" }, "conversation_id": null }
```

响应（注意：被拒绝也是 200，治理结论在响应体里）：

```json
{
  "run_id": "run_830791cf9d004836abf3aef2a2e43af0",
  "invocation_id": "...",
  "tool_name": "echo",
  "status": "succeeded",
  "allowed": true,
  "reason": "命中策略规则 ...（allow，priority=100）",
  "result": { "text": "hello" },
  "error_code": null,
  "error_message": null
}
```

`status` 取值：

| status | 含义 |
|---|---|
| `succeeded` | 通过治理并执行成功 |
| `denied` | 在治理阶段被拦下（未注册 / 停用 / 参数不合契约 / 策略拒绝），**没有执行** |
| `failed` | 通过治理但执行失败（超时、实现缺失等） |

调用顺序固定为：**注册表 → JSON Schema → 策略 → 审计 → 执行**，任一步不过都不会进入下一步。
HTTP 状态码只表示请求本身是否被处理；策略拒绝不是 API 错误，因此返回 200 并在响应体中表达。

注意：工具执行成功会**立即提交**其调用记录，之后即便模型调用失败也不会回滚——
真实副作用不应被假装撤销。

注册工具与写策略需要 `admin`；`developer` 只能调用已被放行的工具。

### 大 payload 旁路（artifact.read）

工具结果超过 `TOOL_RESULT_OFFLOAD_CHARS`（默认 4000）时，**进模型上下文的不再是结果本身**，
而是一个信封：

```json
{
  "status": "succeeded",
  "result_omitted": true,
  "artifact": {
    "invocation_id": "…",
    "chars": 23110,
    "sha256": "…",
    "preview": "前 400 字符…"
  },
  "hint": "结果过大已旁路。需要细节时调用 artifact.read(invocation_id, offset, limit)，不要凭 preview 猜测。"
}
```

**数据没有丢**：完整结果仍在 `tool_invocations.result` 里，只是没进模型上下文。
模型用平台内建工具 `artifact.read` 按需取回：

```json
{ "invocation_id": "…", "offset": 0, "limit": 4000 }
```

返回 `{ invocation_id, tool_name, offset, limit, total_chars, text, eof }`——
按字符切片，单次上限同样是 `TOOL_RESULT_OFFLOAD_CHARS`。

要点：

- `artifact.read` 是**平台能力**，不用手工注册（按租户懒注册），在没有策略规则命中时
  默认放行；显式 deny 仍然生效，管理员也可以把它停用；
- 跨租户读不到别人的 `invocation_id`（`TenantScoped` 强制过滤），未命中即 `tool_not_found`；
- `tool_invocations.offloaded` / `result_chars` 两列记录哪些结果被旁路过；
- **SSE 的 `tool.result` 事件仍带全量结果**——帧不占上下文预算，旁路只影响"给模型看什么"。

详见 [features/tool-result-offload.md](features/tool-result-offload.md)。

### 隔离执行器（executor）

注册时 `execution_type=executor`，`config` 只允许声明运行时与镜像：

```json
{
  "name": "run_python",
  "execution_type": "executor",
  "config": {"runtime": "python", "image": "python:3.12-slim"},
  "input_schema": {
    "type": "object",
    "properties": {"command": {"type": "string"}},
    "required": ["command"],
    "additionalProperties": false
  }
}
```

约束（违反返回 422）：

- `config.runtime` ∈ `shell` | `python`；
- `config` 不允许出现 `command` / `argv` / `cmd`——命令必须来自调用参数；
- `input_schema` 必须把 `config.command_argument`（默认 `command`）声明为 `string`
  且列为必填；
- `config.image` 只允许字母、数字与 `. _ - / : @`，且不能以 `-` 开头；
- `config.inputs` 见下方「输入文件挂载」。

调用：`{"arguments": {"command": "print(1+1)"}}`。成功响应节选：

```json
{
  "status": "succeeded",
  "result": {
    "exit_code": 0,
    "stdout": "2\n",
    "stderr": "",
    "stdout_truncated": false,
    "stderr_truncated": false,
    "duration_ms": 412,
    "artifacts": [{"path": "report.txt", "size": 14, "sha256": "...", "artifact_id": "..."}]
  },
  "artifacts": [
    {"id": "...", "path": "report.txt", "size_bytes": 14, "sha256": "...", "tool_name": "run_python"}
  ]
}
```

失败时 `status=failed`，`error_code` 取值：

| error_code | 含义 |
|---|---|
| `tool_execution_failed` | 容器退出码非 0（`error_message` 带截断后的 stderr） |
| `tool_timeout` | 超过工具或 `EXECUTOR_TIMEOUT_SECONDS` 限制 |
| `executor_not_available` | 执行器未启用、找不到可执行文件或 Docker 不可用 |
| `executor_error` | 执行器返回无法解析或不兼容的结果 |
| `tool_arguments_invalid` | 缺少命令参数或参数不符合 schema |

#### 输入文件挂载

`config.inputs` 声明哪些调用参数承载**产物 id**，以及它们要挂到容器里的什么位置：

```json
{
  "config": {
    "runtime": "python",
    "inputs": [
      {"argument": "file_id", "target": "input/data.csv", "required": true}
    ]
  },
  "input_schema": {
    "type": "object",
    "properties": {"command": {"type": "string"}, "file_id": {"type": "string"}},
    "required": ["command"],
    "additionalProperties": false
  }
}
```

- `argument`：工具参数名，模型把**产物 id**（`ToolArtifact.id`）放在这里；
- `target`：容器内 `/workspace` 下的相对路径；
- `required`：缺参时直接失败（`false` 时缺参就少挂一个）。

注册期约束（违反返回 422 `validation_error`）：`inputs` 必须是数组、至多 16 项、
`argument` 匹配 `^[a-z_][a-z0-9_]{0,63}$`、`target` 必须是安全相对路径
（不能含 `..`、绝对路径或盘符，首段不能是 `output`）、`target` 不能重复、
且 `argument` 必须在 `input_schema` 里声明为 `string`。

执行期：来源**只能是当前租户登记过的产物**（`ToolArtifact`），
挂载一律只读（`:ro`），不区分「产物不存在」与「属于别的租户」——
两种情形都是 `status=failed` + `error_code=tool_arguments_invalid`。

详见 [features/executor-input-mounts.md](features/executor-input-mounts.md)。

产物接口：

- `GET /artifacts`：分页列出**本租户**的全部产物索引，可选 `run_id`、`tool_name` 过滤；
  按生成时间倒序。租户隔离由 ORM 强制过滤（`ToolArtifact` 是 `TenantScoped`），
  跨租户不可见。返回 `Page[ToolArtifactRead]`，只含索引不含文件内容。
- `GET /tool-invocations/{invocation_id}/artifacts`：分页列出一次调用的产物索引。
- `GET /artifacts/{artifact_id}/download`：下载文件，固定 `application/octet-stream`
  + `Content-Disposition: attachment`；文件缺失返回 404 `artifact_not_found`。
- 只收集 `/workspace/output` 下的普通文件；不跟随符号链接，拒绝 `../` 与绝对路径；
  大小与 SHA-256 以宿主机重新读取为准。

详见 [features/artifact-center.md](features/artifact-center.md)。

> 隔离由 Docker 提供：`--network none`、`--read-only`、`--cap-drop ALL`、
> `--pids-limit` / `--memory` / `--cpus`、默认非 root（Windows 例外）。
> 执行器本身不是安全边界。

### 观测

- `GET /agents/{agent_id}/tools`：该 Agent 视角下每个工具的可用性（`allowed` + `reason`）。
- `GET /tool-invocations`：调用记录，可按 `run_id`、`tool_name` 过滤。
- `GET /audit-logs`：审计事件，可按 `run_id`、`action` 过滤。
  包含 `tool.register` / `tool.update` / `policy.create` / `tool.invoke` /
  `tool.execute.failed` / `skill.*` / `apikey.*` / `tenant.*` / `membership.*`。
  审计只记录参数**键名**，不记录参数值。
- `GET /audit-logs/verify`：校验当前租户的审计哈希链，返回
  `{ valid, checked, legacy, broken_at, reason }`。

审计日志是只追加的：没有更新/删除接口，ORM 层也会拒绝 UPDATE/DELETE。
`legacy` 是引入哈希链之前写入的行数（未链接，不影响结论）。
注意：删除尾部记录无法被检测（剩余链仍自洽），详见 security.md。

### Agent Loop 中的工具调用

模型可以在一轮回复中提出工具调用，平台会逐个走上述治理流程，并把结果（含拒绝原因）
回灌给模型继续推理，最多 `MAX_TOOL_ITERATIONS`（默认 4）轮。
只有**策略已放行**的工具才会出现在模型可用的 tools 列表里。

## 认证与密钥管理

全部端点需要 `write:keys`（即仅 admin）。

- `POST /api-keys`：`{ name, role }`，响应里包含 **仅出现一次** 的 `api_key` 明文。
- `GET /api-keys`：列表（分页），只返回 `key_prefix`，不含明文或哈希。
- `PATCH /api-keys/{id}`：`{ enabled }`，启停密钥；停用最后一个可用 admin 密钥返回 409。

`/health`、`/docs`、`/openapi.json` 为公开端点。

### 流式（SSE）

`POST /conversations/{id}/messages/stream`，`Content-Type: text/event-stream`。

事件（每条为 `event: <name>` + `data: <json>`）：

| event | data |
|---|---|
| `run.started` | `{ run_id }` |
| `delta` | `{ text }` 增量文本 |
| `tool.call` | `{ tool, arguments }` |
| `tool.result` | `{ tool, status, error_code, result }` |
| `completed` | `{ run_id, user_message, assistant_message }` |
| `error` | `{ code, message }` |

鉴权、配额与会话存在性在**建立流之前**校验，因此那几类失败仍是 401/403/404/429；
一旦开始推送，后续错误只能以 `error` 事件表达（HTTP 状态已是 200）。

运行本身在独立任务里执行：客户端断开只退订，运行继续跑完并落库。

### 断线续传

`GET /runs/{run_id}/events/stream?after=<seq>`，`text/event-stream`。

- 游标来自 `after` 查询参数或标准 `Last-Event-ID` 头（`after` 优先，取较大语义上的后者）。
- 每条事件带 `id: <seq>`；回放从 `seq > after` 开始。
- 运行仍为 `running` 时持续跟随（轮询间隔 `RUN_STREAM_POLL_SECONDS`），
  直到终态或超过 `RUN_STREAM_MAX_SECONDS`，最后发一条 `stream.end`：
  `{ run_id, status, reason? }`（`reason=timeout` 表示跟随超时）。
- 事件类型里 `run.completed` 是运行生命周期记录（含计数），
  `completed` 是带最终消息的交付事件，重连方用它重建结果。
- **增量文本不落库**：重连能拿到生命周期事件与最终消息，但拿不回已流出的那段文字。

### MCP 工具

- `POST /mcp/servers/{server_name}/sync`：连接已声明的 MCP 服务器，把其工具同步进
  注册表。响应 `{ "server", "registered": [Tool...], "skipped": ["<工具名>: <原因>"] }`。
  `inputSchema` 非法的工具会被跳过而不是写入坏契约。
- 同步得到的工具 `name` 形如 `echo.echo`，`execution_type=mcp`，
  `config={"server": "...", "tool": "..."}`。
- 注册 MCP 工具时，`config.server` 必须已在 `MCP_SERVERS` 中声明，否则 422；
  执行阶段若服务器已不存在，返回 `failed` + `tool_execution_failed`。

### 技能

- `POST /skills`：创建技能，body `{ name, description, instructions, enabled }`；
  重名 409，名称或指令不合法 422。需要 `write:skills`（仅 admin）。
- `GET /skills`：列表（分页，按名称排序）。
- `GET /skills/{id}`：详情。
- `PATCH /skills/{id}`：更新 `description` / `instructions` / `enabled`。
- Agent 的 `skill_names` 引用已存在的技能名；引用了不存在的技能返回 422
  且 `details.missing` 列出缺失项。

运行时技能内容并入 system prompt，受 `SKILL_MAX_CHARS` 限制；停用或缺失的技能会被跳过，
实际生效列表见 `run.started` 事件的 `skills` 字段。

## 运行记录与限额

- `GET /me`：当前身份（`key_id`、`name`、`role`、`permissions`），供前端决定显示哪些操作。
- `GET /runs`：运行列表（分页），可按 `agent_id`、`conversation_id`、`status` 过滤。
- `GET /runs/{run_id}`：单次运行详情。
- `GET /runs/{run_id}/events`：事件流（按 `seq` 升序）。

事件类型：`run.started`、`context.trimmed`、`model.request`、`model.response`、
`tool.result`、`run.completed`、`run.failed`。

### 限流与配额

| 限制 | 配置 | 计数维度 | 超限 |
|---|---|---|---|
| 请求频率 | `RATE_LIMIT_PER_MINUTE`（默认 120） | 每个 API Key 每分钟 | 429 `rate_limited` + `Retry-After` |
| 运行次数 | `DAILY_RUN_QUOTA`（默认 200） | 每个 API Key 每 24 小时 | 429 `quota_exceeded` |

设为 `0` 即关闭对应限制。限流后端由 `RATE_LIMIT_BACKEND` 选择：
`memory`（进程内，单实例）或 `redis`（多副本共享，需 `REDIS_URL`）；
对外契约（429 + `Retry-After` + 错误码）与后端无关。
失败的运行同样占用配额，避免靠制造失败绕过限额。

## 后续版本

后续增加幂等键、任务取消、人工审批与按工具调用配额。

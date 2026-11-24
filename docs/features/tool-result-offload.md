# 工具结果大 payload 旁路（Artifact Offload）功能设计

> 参考：`HomeMind/docs/octop-ui-payload-offload.md`（Octop #1032，大 payload 旁路）
> 状态：**设计稿，尚未实现**
> 目标版本：V0.3

## 1. 问题

工具返回结果的全部内容会被塞进模型上下文，而上下文预算只有
`CONTEXT_MAX_CHARS=24000`（`backend/app/core/config.py`）。

以 Octop 实测的场景为例：一个搜索类工具返回 22.5 KB JSON，占掉 24000 字符预算的
**94%**。本项目里这会立刻触发三个后果：

1. `build_messages`（`backend/app/services/context.py:34`）按**整条消息**丢弃历史，
   一条大结果会把前面所有对话挤出去，用户会感觉"Agent 失忆了"；
2. 工具循环最多 4 轮（`MAX_TOOL_ITERATIONS=4`），大结果在后续每一轮都要重新发送；
3. 结果本身可能仍然超预算，`build_messages` 无法切分单条消息，只能整条丢或整条留。

## 2. 根因：同一个字符串服务三类消费者

| 消费者 | 需求 | 现状 |
|---|---|---|
| 模型上下文 | 越小越好 | `_tool_result_message` 把 `outcome.result` 全量序列化（`agent_runtime.py:542`） |
| 运行记录 | 需要完整数据可回溯 | `ToolInvocation.result` 已存全量 JSON（`tool_gateway.py:91`），按 `run_id` 可查 |
| SSE / 前端 | 展示状态即可 | `tool.result` 事件带全量 `result`（`agent_runtime.py:269-276`） |

模型的诉求（小）与记录的诉求（全）是对立的。**截断不可行**——前端和追溯都需要完整数据。
所以必须在「工具执行后、消息进模型前」把**数据面**（大 payload）与**控制面**（状态 + 摘要 + 引用）
分离，而不是压缩。

关键有利条件：**数据面已经在数据库里了**。`ToolInvocation.result` 保存了每次调用的完整结果，
`ToolCallOutcome.invocation_id`（`tool_gateway.py:42`）也已经把它带回给运行时。
所以这个功能不需要新表，只需要"模型侧改用引用"。

## 3. 已验证事实

| # | 事实 | 证据 |
|---|---|---|
| F1 | 工具结果全量进模型消息 | `agent_runtime.py:542-556` `_tool_result_message` |
| F2 | 上下文按整条消息裁剪，不能切分单条 | `context.py:34-80` `build_messages` |
| F3 | 上下文预算 24000 字符，工具循环最多 4 轮 | `config.py` `context_max_chars` / `max_tool_iterations` |
| F4 | 每次工具调用都已持久化完整结果 | `tool_gateway.py:91` `ToolInvocation(... result=...)` |
| F5 | 调用结果带 `invocation_id`，运行时能拿到 | `tool_gateway.py:41-49` `ToolCallOutcome` |
| F6 | 审计只记参数键名，不记参数值/结果 | `app/services/audit.py`、`tool_gateway.py` 审计写入点 |
| F7 | 内建工具白名单只有 `echo` / `current_time`，注册不产生执行能力 | `app/services/tool_executor.py` |
| F8 | SSE `tool.result` 带全量结果；前端只渲染工具名与状态 | `agent_runtime.py:269-276`、`frontend/src/App.tsx` |
| F9 | 租户隔离由 ORM 事件统一施加，`ToolInvocation` 属于 `TenantScoped` | `app/db/tenancy.py`、`app/models/entities.py` |

## 4. 方案

### 4.1 剥离条件

在 `_tool_result_message` 构造消息**之前**判断，**全部满足**才剥离，任一不满足保持现状：

1. `outcome.status == "succeeded"`（失败/被拒的结果通常很小，且错误信息对模型有价值）；
2. `outcome.result` 可 JSON 序列化，且序列化后长度 ≥ `TOOL_RESULT_OFFLOAD_CHARS`（默认 **4000**，可配）；
3. `outcome.result` 不是"本身就很小的结构"——即去掉后剩余信封仍有意义（避免剥完只剩 `{"status":"succeeded"}`）。

不再额外判断"是否是 UI payload"：本项目没有 `octop_ui` 那一层，任何大结果都适用。

### 4.2 模型侧信封

```jsonc
{
  "status": "succeeded",
  "result_omitted": true,
  "artifact": {
    "invocation_id": "…",     // 可直接用于 artifact.read
    "chars": 23110,           // 原结果序列化长度
    "sha256": "…",            // 便于校验/去重
    "preview": "…"            // 前 400 字符，让模型知道大致形状
  },
  "hint": "结果过大已旁路。需要细节时调用 artifact.read(invocation_id, offset, limit)，不要凭 preview 猜测。"
}
```

设计取舍：

- **保留 preview 与 chars**：模型能判断"这份结果值不值得再读一次"，而不是盲调工具。
- **不带 data 本体**：这是省上下文的关键。
- **显式 hint**：模型不知道有这个机制就会胡编，必须写在内容里而不是只写在提示词里。

### 4.3 取回通道：`artifact.read`

新增内建工具 `artifact.read`，参数 `{invocation_id, offset, limit}`，返回该次调用结果的一段切片。

- 只在**存在已旁路结果**时把工具描述注入模型（避免每轮都多一个工具）；
- 读取前校验 `invocation_id` 属于当前租户（`TenantScoped` 自动过滤，未命中即 404）；
- 单次返回上限同样是 `TOOL_RESULT_OFFLOAD_CHARS`，防止"读回来又爆"；
- 走与其它工具**完全相同**的治理链：schema 校验 → policy → 审计 → 执行。
  它是只读的，因此策略上默认允许，但审计照记（`artifact.read` + invocation_id）。

这是与 Octop 最大的差异：Octop 把"模型侧分页"列为后续增强，我们直接做成通用能力，
因此**旁路不损失能力**——模型想看细节随时可以取。

### 4.4 SSE 与前端

`tool.result` 事件保持**全量**：

- 前端目前只显示工具名与状态（F8），带全量不构成渲染问题；
- 调试时能直接在事件流里看到原始结果；
- 更重要的是：SSE 帧不参与上下文预算，没有剥离动机。

若后续要做"结果详情"面板，可改为带 `invocation_id` 的引用 + `GET /tool-invocations/{id}`，
但那属于独立需求，不在本次范围。

### 4.5 与现有治理链的关系

顺序不变，仍然必须：

```text
注册表 → JSON Schema 校验 → 服务端 policy → 审计 → 执行 → 【本次新增：旁路判定】 → 模型消息
```

旁路只影响"给模型看什么"，**不能**影响"是否允许执行""是否留审计"。
被拒绝、失败、越权的调用一律不旁路——这些信息本来就该完整回到模型。

## 5. 兼容性与风险

| 风险 | 评估 | 缓解 |
|---|---|---|
| 模型看不到细节而编造 | 中 | 信封里有 hint + preview；`artifact.read` 可随时取 |
| 多一次工具调用 | 低 | 仅在模型确实需要细节时发生；比每轮重发 22KB 更省 |
| 阈值过低导致琐碎剥离 | 中 | 默认 4000 字符，可配；测试覆盖"小结果不动" |
| 剥离后审计/追溯缺数据 | 无 | 完整结果仍在 `ToolInvocation.result` |
| 跨租户读取 artifact | 无 | `ToolInvocation` 属 `TenantScoped`，由 ORM 事件强制过滤 |
| 老消息兼容 | 无 | 不新增列、不改历史消息结构；仅新消息带信封 |
| `artifact.read` 成为新的攻击面 | 低-中 | 只读、仅限本租户、参数为整数偏移量、有单次上限、走完整治理链 |

## 6. 测试计划

单元（`backend/tests/`）：

- 大结果被剥离：消息里没有原 payload，有 `artifact.invocation_id` 与 `preview`；
- 小结果（< 阈值）原样返回；
- 失败/被拒结果不剥离；
- 正好等于阈值、以及 `result` 为 `None` 的边界；
- 信封可 JSON 序列化，且长度显著小于原结果。

集成：

- MCP 工具返回 ~20KB → 一次运行内 `state.messages` 中工具消息长度 < 2000 字符；
- 模型（Mock）调用 `artifact.read` 后能拿到完整切片，且切片拼起来等于原结果；
- `artifact.read` 的租户隔离：用另一个租户的 invocation_id 读取返回 404；
- 审计记录包含 `artifact.read` 且不含结果内容。

回归：

- 现有 352 条测试全部仍通过（剥离默认开启时不得改变小结果的既有断言）。

## 7. 明确不做

- 不新增 artifacts 表：完整结果已经在 `ToolInvocation.result`；
- 不改 SSE 与前端渲染；
- 不做服务端摘要（用 preview，不做 LLM 摘要——那会引入额外的模型调用成本与不确定性）；
- 不做跨运行的结果复用/去重（`sha256` 只用于校验，不建索引）；
- 不为超大结果（> 1MB）引入文件通道，后续需要再议。

## 8. 实施顺序

1. **PR-1**：`artifact.read` 内建工具 + 治理链接入 + 租户隔离测试（无副作用，可独立合并）；
2. **PR-2**：`_tool_result_message` 旁路逻辑 + 配置项 + 单元/集成测试；
3. **PR-3**：文档（api-contract、security、README）与可观测性（span 属性记 `artifact.offloaded`）。

先做 PR-1 的理由：它不改现有行为，却能先验证"模型能否用起来"这个最大的不确定性。

## 附录：与 Octop 方案对照

| 维度 | Octop | 本项目 |
|---|---|---|
| 剥离对象 | `octop_ui.data`（UI payload） | 任意超过阈值的结果 |
| 存放位置 | `ToolMessage.artifact`（随消息持久化） | 复用 `ToolInvocation.result`（已有） |
| 模型取回 | 无（列为后续增强） | `artifact.read` 分页工具 |
| 前端通道 | WS 帧带 artifact，需改多处解析 | SSE 保持全量，无需改动 |
| 剥离时机 | 中间件最内层 | 消息构造前，治理链之后 |
| 主要风险 | 前后端必须同批发布 | 模型多一次工具调用 |

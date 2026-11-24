# 工具结果大 payload 旁路（Artifact Offload）

> 参考：`HomeMind/docs/octop-ui-payload-offload.md`（Octop #1032，大 payload 旁路）
> 状态：**已实现**
> 目标版本：V0.3

## 1. 问题

工具返回结果的全部内容会被塞进模型上下文，而上下文预算只有
`CONTEXT_MAX_CHARS=24000`（`backend/app/core/config.py`）。

以 Octop 实测的场景为例：一个搜索类工具返回 22.5 KB JSON，占掉 24000 字符预算的
**94%**。本项目里这会立刻触发三个后果：

1. `build_messages`（`backend/app/services/context.py`）按**整条消息**丢弃历史，
   一条大结果会把前面所有对话挤出去，用户会感觉"Agent 失忆了"；
2. 工具循环最多 4 轮（`MAX_TOOL_ITERATIONS=4`），大结果在后续每一轮都要重新发送；
3. 结果本身可能仍然超预算，`build_messages` 无法切分单条消息，只能整条丢或整条留。

## 2. 根因：同一个字符串服务三类消费者

| 消费者 | 需求 | 现状 |
|---|---|---|
| 模型上下文 | 越小越好 | `_tool_result_message` 把 `outcome.result` 全量序列化 |
| 运行记录 | 需要完整数据可回溯 | `ToolInvocation.result` 已存全量 JSON，按 `run_id` 可查 |
| SSE / 前端 | 展示状态即可 | `tool.result` 事件带全量 `result` |

模型的诉求（小）与记录的诉求（全）是对立的。**截断不可行**——前端和追溯都需要完整数据。
所以必须在「工具执行后、消息进模型前」把**数据面**（大 payload）与**控制面**（状态 + 摘要 + 引用）
分离，而不是压缩。

关键有利条件：**数据面已经在数据库里了**。`ToolInvocation.result` 保存了每次调用的完整结果，
`ToolCallOutcome.invocation_id` 也已经把它带回给运行时。所以这个功能不需要新表，
只需要"模型侧改用引用"。

## 3. 方案

### 3.1 剥离条件

在 `_tool_result_message` 构造消息**之前**判断，**全部满足**才剥离，任一不满足保持现状：

1. `outcome.status == "succeeded"`（失败/被拒的结果通常很小，且错误信息对模型有价值）；
2. `outcome.result` 可 JSON 序列化，且序列化后长度 ≥ `TOOL_RESULT_OFFLOAD_CHARS`（默认 **4000**，可配）；
3. 开关 `TOOL_RESULT_OFFLOAD_ENABLED` 打开（默认开）。

不再额外判断"是否是 UI payload"：本项目没有 `octop_ui` 那一层，任何大结果都适用。

### 3.2 模型侧信封

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

### 3.3 取回通道：`artifact.read`

内建工具 `artifact.read`，参数 `{invocation_id, offset, limit}`，返回该次调用结果的一段切片：

```json
{
  "invocation_id": "…",
  "tool_name": "echo",
  "offset": 0,
  "limit": 4000,
  "total_chars": 23110,
  "text": "…切片…",
  "eof": false
}
```

- 单次返回上限同样是 `TOOL_RESULT_OFFLOAD_CHARS`，防止"读回来又爆"；
- 读取前校验 `invocation_id` 属于当前租户（`TenantScoped` 自动过滤，未命中即 404）；
- 走与其它工具**完全相同**的治理链：schema 校验 → policy → 审计 → 执行。

这是与 Octop 最大的差异：Octop 把"模型侧分页"列为后续增强，我们直接做成通用能力，
因此**旁路不损失能力**——模型想看细节随时可以取。

### 3.4 什么时候把工具暴露给模型

工具规格在运行开始时固定，但旁路发生在运行**中**——所以规格必须能动态追加：

- 旁路发生的那一刻，把 `artifact.read` 的规格追加进本次运行的 `tool_specs`，
  下一轮模型请求就能看见它；
- 若该会话此前已经出现过旁路结果，新一轮对话开始时就直接带上这个工具
  （模型可以回头把上一轮的细节读回来）；
- 没有任何旁路结果时，模型**看不到**这个工具——不会平白多一个可调用项。

### 3.5 与现有治理链的关系

顺序不变，仍然必须：

```text
注册表 → JSON Schema 校验 → 服务端 policy → 审计 → 执行 → 【旁路判定】 → 模型消息
```

旁路只影响"给模型看什么"，**不能**影响"是否允许执行""是否留审计"。
被拒绝、失败、越权的调用一律不旁路——这些信息本来就该完整回到模型。

### 3.6 SSE 与前端

`tool.result` 事件保持**全量**：

- 前端目前只显示工具名与状态，带全量不构成渲染问题；
- 调试时能直接在事件流里看到原始结果；
- 更重要的是：SSE 帧不参与上下文预算，没有剥离动机。

## 4. 实现位置

| 关注点 | 位置 |
|---|---|
| 剥离判定、信封、取回 | `backend/app/services/tool_offload.py` |
| 内建工具注册（按租户懒注册） | `tool_offload.ensure_artifact_read_tool` |
| 策略默认放行 | `backend/app/services/policy.py`（无规则命中时） |
| 网关执行分支 | `backend/app/services/tool_gateway.py`（`artifact.read`） |
| 运行时旁路与规格注入 | `backend/app/services/agent_runtime.py`（`_maybe_offload` / `_expose_artifact_read`） |
| 模型 | `tool_invocations.offloaded` / `result_chars` 两列（迁移 `b3d9a1c47e20`） |

## 5. 与设计稿的两处偏离

1. **加了两列，不是新表**：完整结果本来就在 `tool_invocations.result` 里，但需要标记
   "这次结果被旁路了"以及"它有多大"，否则无法回答"哪些运行被瘦身过"。
   加两列比建一张 artifacts 表轻得多。
2. **工具是平台自动注册的，不是用户注册**：设计稿写的是"注册 + 策略放行"，
   但要求每个用户手工注册一个平台能力会被误当成"功能坏了"。因此
   `artifact.read` 由平台在当前租户**懒注册**（首次调用时建好，已存在则不动——
   管理员显式停用它时不会被重建），并且在**没有任何策略规则命中时默认放行**
   （显式 deny 仍然生效）。

## 6. 测试

单元（`backend/tests/test_tool_offload.py`）：

- 剥离判定：阈值边界（≥4000 剥离、3999 不剥离）、失败与被拒不剥离、
  不可序列化不剥离、开关关闭不剥离；
- 信封：可 JSON 序列化、长度远小于原结果、含 invocation_id / chars / sha256 / preview / hint；
- 小结果原样回灌（既有行为不被改写）。

集成：

- 15KB 工具结果 → 模型看到的工具消息 < 2000 字符，且原始 payload 一个字都不在里面；
- 旁路后 `ToolInvocation.result` 仍存完整数据，并被标记 `offloaded=True` + `result_chars`；
- 模型从信封里取出 invocation_id，调用 `artifact.read` 能拿回切片（`total_chars` 与
  原文一致、`eof` 正确）；
- 跨租户读取：另一个租户用别人的 invocation_id 调用 → `tool_not_found`；
- 没有旁路时模型看不到 `artifact.read`。

## 7. 明确不做

- 不新增 artifacts 表：完整结果已经在 `ToolInvocation.result`；
- 不改 SSE 与前端渲染；
- 不做服务端摘要（用 preview，不做 LLM 摘要——那会引入额外的模型调用成本与不确定性）；
- 不做跨运行的结果复用/去重（`sha256` 只用于校验，不建索引）；
- 不为超大结果（> 1MB）引入文件通道，后续需要再议；
- 不做结果详情面板（那属于独立需求）。

## 附录：与 Octop 方案对照

| 维度 | Octop | 本项目 |
|---|---|---|
| 剥离对象 | `octop_ui.data`（UI payload） | 任意超过阈值的结果 |
| 存放位置 | `ToolMessage.artifact`（随消息持久化） | 复用 `ToolInvocation.result`（已有） |
| 模型取回 | 无（列为后续增强） | `artifact.read` 分页工具 |
| 前端通道 | WS 帧带 artifact，需改多处解析 | SSE 保持全量，无需改动 |
| 剥离时机 | 中间件最内层 | 消息构造前，治理链之后 |
| 主要风险 | 前后端必须同批发布 | 模型多一次工具调用 |

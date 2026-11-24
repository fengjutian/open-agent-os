# 会话模式 Ask / Plan / Craft

> 对标：Octop 的 `octop_harness.middleware.conversation_mode`
> 状态：**已实现**

## 1. 要解决什么

同一个 Agent，有时需要它直接动手，有时只希望它回答问题，有时希望它先给方案再执行。
把这种差别塞进提示词是不可靠的——模型仍可能去调用有副作用的工具。
可靠的做法是**在工具可见性上做限制**：模型看不到的工具，它就不会调用。

## 2. 语义（与 Octop 对齐）

| 模式 | 工具可见性 | 提示词 |
|---|---|---|
| `craft`（默认） | 全部（仍受 policy 约束） | 无附加 |
| `ask` | 只读集合 | 只读作答，需要变更就提示切换模式 |
| `plan` | 只读集合 | 探索后产出分步计划，停下等确认 |

只读集合 = 内建白名单（`current_time`）+ 管理员显式标记 `read_only` 的工具。
未知模式值一律回落服务端默认值（`craft`）——宁可多给能力，也不要误判成受限模式。

## 3. 与 Octop 的差异

Octop 的 `plan` 模式允许模型写 `plans/<slug>.md`（它有文件工具）。我们没有文件写工具，
所以 `plan` 与 `ask` 的工具集合相同，区别在提示词：一个要求产出计划并停下，一个是问答。
等以后加了文件工具，再按「只许写 plans/*.md」的方式扩展即可。

## 4. 实现位置

- `backend/app/services/conversation_modes.py`：模式常量、`normalize_mode`、
  `filter_tool_specs`、`mode_hint`；
- `backend/app/models/entities.py`：`Conversation.mode`、`ToolDefinition.read_only`；
- `backend/app/services/agent_runtime.py`：`_start()` 解析模式 → 过滤工具 → 注入提示词，
  并把 `mode` 写进 `run.started` 事件；
- `backend/app/api/routes.py`：创建会话带 `mode`、`PATCH /conversations/{id}`、
  发送消息时的单轮覆盖；
- `frontend/src/App.tsx`：会话头部三档切换（写入服务端，刷新后保持）。

## 5. 为什么要过滤而不是"请求后拒绝"

被裁掉的工具不在 `tools` 参数里，模型无从请求；若改成"先给它、再拒绝"，
会多一轮往返、把拒绝原因写进上下文，还可能诱导模型反复尝试。过滤的成本更低、行为更确定。

## 6. 边界

- 模式只影响**模型看得到什么**，不改变 policy / 审计 / 租户隔离；
- 单轮覆盖（`mode` 放在消息里）不会改写会话的粘性设置；
- `read_only` 是**人工声明**，注册 MCP 工具时无法自动推断副作用，必须由管理员判断。

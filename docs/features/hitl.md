# 人工审批（HITL）

> 对标：Octop 的 `infra/gateway/hitl`、`agents/security/hitl_session`
> 状态：**已实现**

## 1. 要解决什么

有些工具（写文件、发消息、执行命令、花钱的接口）不该由模型自己决定就执行。
需要一个人先看一眼参数，点批准或拒绝。纯前端拦不住——模型走的是服务端工具链，
所以"暂停点"必须在服务端。

## 2. 语义

- `ToolDefinition.requires_approval=True` 的工具，每次执行前都会停；
- 策略仍然是**先判**的：策略拒绝就直接拒绝，不会进入审批（审批不是绕过策略的通道）；
- 批准 → 执行工具，把结果回灌给模型，**从检查点续跑同一个 run**；
- 拒绝 → 不执行，把"被拒绝"作为工具结果回灌，让模型自己收尾（而不是硬失败）；
- 审批结论（谁、什么时候、什么理由）进审计，且**不能被二次决定**（409）。

## 3. 三个关键设计

**① 检查点在数据库里，不依赖进程存活。**
暂停时把模型的 `messages` 列表和轮次写进 `runs.checkpoint`，把 run 置为
`waiting_approval`。**服务重启后审批依然有效**——这是"人能慢慢决定"的前提。

**② 同一批 tool_calls 必须全部补齐结果。**
检查点最后一条是模型的 `assistant(tool_calls)` 消息；真实模型要求每个 `tool_call_id`
都有对应的 tool 消息。所以续跑时会把这一批里**每个**调用都补上结果：
被批准的那个用实际执行结果，其余重新走网关（可能再次触发审批）。

**③ 一次逻辑调用只留一条调用记录。**
批准后重放会带上原 `invocation_id`，网关复用那条 `ToolInvocation`，
不会出现"pending_approval + succeeded"两行。

## 4. 交互形态

| 入口 | 行为 |
|---|---|
| `POST /conversations/{id}/messages` | 需要审批时返回 **202** + `{run_id, approval_id, tool, status}` |
| 流式 `/messages/stream` | 发出 `run.waiting_approval` 事件后结束本次流；run 停在 `waiting_approval` |
| `GET /approvals?status=pending` | 待审批列表 |
| `POST /approvals/{id}/approve` | 执行并续跑，返回最终消息（若又有工具需要审批则再返回 202） |
| `POST /approvals/{id}/reject` | 不执行，回灌拒绝原因后继续 |

前端：会话里遇到审批会自动跳到「审批」视图并提示；批准后回到对话即可看到最终回答。

## 5. 明确不做

- **不做超时自动拒绝**：待审批会一直挂着。真实部署需要清理策略（例如 24 小时后标记过期）。
- **不做多级审批**：一个人批了就算通过。
- **不做"部分批准"**：同一批 tool_calls 里如果两个都要审批，会依次产生两条待审批记录。
- 审批人只需要 `write:agents` 权限，没有单独的角色。

## 6. 实现位置

| 关注点 | 位置 |
|---|---|
| 审批门（网关层） | `backend/app/services/tool_gateway.py` |
| 暂停/续跑/检查点 | `backend/app/services/agent_runtime.py` |
| 状态常量与异常 | `backend/app/services/approvals.py` |
| 接口 | `backend/app/api/approval_routes.py` |
| 数据模型 | `ToolApproval`、`Run.checkpoint`、`ToolDefinition.requires_approval` |
| 前端 | `panels.tsx` `ApprovalPanel`、`App.tsx` 的「审批」视图与提示条 |

## 7. 测试

- 暂停：run 停在 `waiting_approval`、检查点非空、审批记录含参数与 call_id、
  工具**未执行**（invocation 仍是 pending_approval）；
- 批准：工具执行成功、run 变 succeeded、模型第二轮看到工具结果、审计留痕；
- 拒绝：工具未执行、run 仍能 succeeded、模型看到的是 rejected 与拒绝原因；
- 重复决定返回 409、未知审批 404、viewer 无权限 403、跨租户不可见（404）；
- 流式：收到 `run.waiting_approval` 事件且 run 状态正确；
- 浏览器 e2e：注册带审批的工具 → 发 `CALL:echo {"text":"hi"}` → 自动跳到审批视图 →
  点"批准并执行" → 待审批清零（截图 `docs/screenshots/approval.png`）。

# 子代理

> 对标：Octop 的 `infra/agents/subagents`、`api/routers/subagents`
> 状态：**已实现**

## 1. 要解决什么

一个 Agent 的上下文被它自己的会话占满了，但有些子任务需要另一个"角色"去做：
用另一个 system prompt、用另一套工具授权、生成自己的运行记录。
让主 Agent 用同一份上下文硬做，既挤占预算，也没法单独审计这次子任务。

子代理 = **派生一次完整的运行**：自己的会话、自己的 Run、自己的上下文预算。
主运行只拿到最终回答。

## 2. 怎么用

1. 注册内置工具 `spawn_subagent`（和普通工具一样要注册，注册本身不产生能力）：

   ```json
   {
     "name": "spawn_subagent",
     "execution_type": "builtin",
     "input_schema": {
       "type": "object",
       "properties": {"agent_id": {"type": "string"}, "task": {"type": "string"}},
       "required": ["task"],
       "additionalProperties": false
     }
   }
   ```

2. 给调用方 Agent 放行（deny-by-default 仍然生效）；
3. 模型调用它时传 `agent_id` + `task`，平台会为目标 Agent 新建会话并跑一次，
   把结果（`answer` / `conversation_id` / `run_id`）作为工具结果回灌。

## 3. 三条硬约束

**① 必须换 `run_id`。** 子运行若沿用父运行的 `run_id`，会直接撞 `runs` 主键。
这在实现里是显式的：`run_id_var.set(f"run_{uuid4().hex}")`。

**② 深度上限。** A 派 B、B 再派 C 是合理的；无限套娃不是。默认 `SUBAGENT_MAX_DEPTH=2`，
超限时返回一个**失败的工具结果**（模型能看到原因并收尾），而不是抛异常打断整条链。

**③ 目标 Agent 必须存在且同租户。** 租户过滤由 ORM 事件保证；查不到就是
`not_found` 的工具结果。

## 4. 为什么放在网关而不是工具实现里

`execute_tool()` 的内置实现是**同步函数**、拿不到数据库会话，而子代理要建会话、
跑一次异步运行。所以 `spawn_subagent` 在网关里有单独分支——它属于平台能力
（派生运行），不是"某个函数的实现"。

## 5. 安全边界

子代理实质上是"以另一个 Agent 的身份消耗模型额度"。因此：

- 工具必须被**注册 + 策略放行**，与其它工具一致；
- 每次委派都有独立的 `Run` 记录与 `tool_invocations` 记录，可追溯；
- 目标 Agent 若被标记 `requires_approval` 的工具，仍会在子运行里触发审批；
- 子运行的会话独立，主会话历史里只留下一条工具结果。

## 6. 测试

- 正常委派：子会话被创建、父子 `run_id` 不同、最终回答来自主模型的汇总；
- 结果回灌：主模型第二轮看到的 tool 消息里含子代理的 `answer`；
- 目标 Agent 不存在：返回失败结果，对话不中断；
- 空任务：拒绝并给出可读原因；
- 深度上限：A→B→B 的第三层被拒，整条链正常收尾；
- 可配置深度：`SUBAGENT_MAX_DEPTH=1` 时只有一层。

## 7. 明确不做

- 不做并行多子代理（一次只派生一个，串行）；
- 不做子代理之间的消息传递（只能经父运行汇总）；
- 不做子代理的工具白名单隔离（用的是目标 Agent 自己的策略）；
- 不做委派预算上限（子运行消耗的额度计入同一个 key 的日配额）。

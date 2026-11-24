# 执行器输入文件挂载（只读）

> 对标对象：Octop「工具产物作为下一次执行器的输入文件」（V0.3 Execution 补齐项）
> 状态：**已实现**
> 目标版本：V0.3

## 1. 问题

隔离执行器（`execution_type=executor`）目前只能执行命令、收集产物，
**没有把已有产物喂回给下一次执行**的通道。

实际场景很常见：

1. 工具 A 跑了一次容器，产出 `report.csv`（落在 `/workspace/output`，已登记为产物）；
2. 用户/Agent 想让工具 B 拿这份 CSV 再算一遍。

当前只有两条路，都不成立：

- **让模型把文件内容读出来再传回去**——大文件直接撑爆上下文预算
  （见 [tool-result-offload.md](tool-result-offload.md) 里那 22.5 KB 的例子）；
- **让模型指定宿主机路径**——那等于把宿主机的文件系统整个交出去。

## 2. 根因

隔离执行器的安全模型是「**容器里跑的命令由模型给，但容器能碰到的东西由平台给**」。
`--network none`、`--read-only`、只挂 workspace、默认非 root——
这些约束都在保护「容器能碰到什么」这一面。

但「容器能碰到什么」这一面**缺了一个正向通道**：平台已经有产物的完整生命周期
（收集 → 落盘 → 登记 `ToolArtifact` → 索引 → 下载），却只有"取回给人看"的口子，
没有"取回给容器用"的口子。

于是每次需要复用文件，都得绕回"把内容塞进模型上下文"——**用最贵、最不可靠的通道
干文件搬运的活**。

## 3. 方案

### 3.1 声明：工具自己声明要哪些输入

`config.inputs` 是唯一的声明入口，注册时校验（`backend/app/api/tool_routes.py:177`
`_assert_executor_inputs`）：

```json
{
  "name": "analyze_csv",
  "execution_type": "executor",
  "config": {
    "runtime": "python",
    "inputs": [
      {"argument": "file_id", "target": "input/data.csv", "required": true}
    ]
  },
  "input_schema": {
    "type": "object",
    "properties": {
      "command": {"type": "string"},
      "file_id": {"type": "string"}
    },
    "required": ["command"],
    "additionalProperties": false
  }
}
```

- `argument`：工具参数名，模型把**产物 id** 放在这里；
- `target`：容器内 `/workspace` 下的相对路径；
- `required`：缺参时直接失败（`required=false` 时缺参就少挂一个）。

注册期就拒掉的（422）：`inputs` 不是数组、超过 16 项、`argument` 不匹配
`^[a-z_][a-z0-9_]{0,63}$`、`target` 不安全、`target` 重复、
以及**`argument` 没有在 `input_schema` 里声明为 `string`**——
最后一个最要紧：参数不是 string，模型根本没法把产物 id 传进来。

### 3.2 来源白名单：只能是本租户登记过的产物

`resolve_input_mounts`（`backend/app/services/artifacts.py:257`）只认数据库里的
`ToolArtifact` 记录：

```python
artifact = self._db.get(ToolArtifact, artifact_id)   # artifacts.py:151
if artifact is None:
    return None
return stored_path(artifact)
```

**租户隔离靠 ORM 强制过滤**（`backend/app/db/tenancy.py` 的 `do_orm_execute` 事件
把租户条件注入所有 SELECT，`Session.get()` 也在内），不靠调用方自觉：
别人的产物 id 在这里查不到，所以**挂不上**。

找不到与「属于别的租户」返回**完全一致的措辞**（`输入产物 {id} 不存在或不可用`），
跨租户时不该泄露这个 id 是否存在。

落盘路径还要再过一次包含性校验（`stored_path`）：必须在产物根目录内、必须是普通文件。

### 3.3 目标路径：两侧各校验一次

| 侧 | 函数 | 规则 |
|---|---|---|
| Python | `safe_input_target`（`artifacts.py:235`） | 非空、≤200 字符、无绝对路径、无 `..`/`.`/空片段、无盘符、首段不是 `output` |
| Rust | `check_input_target`（`rust-executor/src/protocol.rs:204`） | 同上，另加拒绝反斜杠 |

**两侧各做一遍是纵深防御**：Python 侧防的是"注册时声明了脏路径"，
Rust 侧防的是"有人绕过 Python 直接调执行器"。

`output` 被排除是有具体理由的：产物就是从 `<workspace>/output` 收集的，
往那里挂输入会遮住产物收集目录。

Rust 侧还额外校验来源：`symlink_metadata` 必须是普通文件
（符号链接本身就不是普通文件，直接拒掉），目标不能重复，
数量不能超过 16（`check_inputs`，`protocol.rs:229`）。

### 3.4 只读挂载

Rust 侧 `build_args`（`rust-executor/src/docker.rs:100`）在 workspace 挂载**之后**
追加输入挂载：

```text
-v <workspace>:/workspace:rw
-v <source>:/workspace/<target>:ro
```

放在 workspace 之后是必要的：Docker 按出现顺序处理挂载，更具体的路径才会生效。
`:ro` 是硬要求——容器里的命令由模型给，可写就等于可改宿主机的源文件。

### 3.5 错误映射

| 场景 | 结果 |
|---|---|
| 注册期声明不合法 | 422 `validation_error` |
| 缺必填输入 | `status=failed`，`error_code=tool_arguments_invalid` |
| 产物不存在 / 跨租户 / 文件已丢失 | `status=failed`，`error_code=tool_arguments_invalid` |

执行期的失败**不区分**"不存在"和"属于别的租户"，两者措辞完全一致。

## 4. 边界（不做什么）

- **不支持挂任意宿主机路径**。来源只能是 `ToolArtifact`。
  这是刻意的：模型给的命令 + 任意路径 = 宿主机文件系统泄露。
- **不支持目录挂载**。Rust 侧只接受普通文件，符号链接也拒。
- **不支持写回**。挂载一律 `:ro`，容器改不了源文件。
- **不支持挂到 `output/`**。产物收集目录不能被输入遮住。
- **不改变治理链**。输入挂载发生在参数校验与策略放行**之后**，照样过 Schema →
  Policy → 审计；被拒的调用根本走不到挂载这一步。
- **不做网络白名单**。那是另一个特性（V0.3 剩余项），且需要动宿主机 iptables
  或引入代理服务，风险与复杂度不划算。
- **不限制单个产物体积**。上限沿用执行器已有的 `max_artifact_bytes`
  与产物总预算，不在这里另立一套。

## 5. 验证

- `backend/tests/test_executor_inputs.py`：35 条，覆盖目标路径校验（纯函数）、
  注册期 6 类拒绝、端到端挂载（断言执行器收到的 `source`/`target`、
  来源确实是产物目录里的那个文件）、必填缺失、未知产物、
  **跨租户产物挂不上且措辞与"不存在"完全一致**。
- `rust-executor/src/protocol.rs` 单测 4 条：合法挂载、逃逸目标、
  来源缺失/非普通文件、目标重复。
- `rust-executor/src/docker.rs` 单测 1 条：断言 docker 参数里是
  `<source>:/workspace/<target>:ro`，且没有可写形式。
- 假执行器新增 `echo_inputs` 模式，把收到的 `inputs` 原样回显，
  用来验证「Python → 协议 → 执行器」这一段没丢东西。

## 6. 部署注意

**Python 后端与 Rust 执行器必须同步升级。**
`ExecutionRequest` 上有 `#[serde(deny_unknown_fields)]`
（`rust-executor/src/protocol.rs:67`），新后端会发 `inputs` 字段，
旧二进制会因为不认识这个字段而直接拒绝请求，表现为
`error_code=executor_error`。升级顺序建议先升 Rust 二进制，再升后端。

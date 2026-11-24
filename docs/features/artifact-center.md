# 产物中心（租户级产物列表 + 前端界面）

> 对标对象：Octop「工作区文件 / 产物浏览」（V0.3 Execution 配套）
> 状态：**已实现**
> 目标版本：V0.3

## 1. 问题

隔离执行器产出的文件落在 `ARTIFACT_DIR/<tenant>/<invocation>/…`，
数据库里有一条 `ToolArtifact` 索引记录。但**只有一条路能碰到它们**：

```
GET /tool-invocations/{invocation_id}/artifacts
```

也就是说：**必须先知道某一次调用的 `invocation_id`，才能看到那一次的产物。**

用户手上通常只有「某次运行」或「某个工具」，没有 invocation id。于是：

- 跑完 20 次容器，想找其中一份 CSV，找不到；
- 想把一份产物喂给下一个工具（见
  [executor-input-mounts.md](executor-input-mounts.md)），需要拿到产物的 id，
  但界面里根本没有地方能看到这个 id。

后端其实早就有 `GET /artifacts/{artifact_id}/download`，
**下载接口存在、列表接口不存在**——这是明显的缺口。

## 2. 根因

产物索引的查询入口是按 invocation 组织的，因为产物是「一次调用的产物」，
`invocation_id` 天然是它的归属键。

但**归属键不等于检索维度**。用户脑子里的检索维度是：
「这个租户最近产出了什么」「这个工具产出了什么」「这次运行产出了什么」，
而不是「第几次调用产出了什么」。

`ToolInvocation` 与 `ToolArtifact` 一对一不到（一次调用可以产出多个文件，
也可以一个都没有），所以拿 `invocation_id` 当唯一入口，
等于**强迫用户去猜一个内部 id**。

## 3. 方案

### 3.1 后端：租户级列表接口

`GET /api/v1/artifacts`（`backend/app/api/tool_routes.py` `list_artifacts`）：

- 可选过滤：`run_id`、`tool_name`；
- 分页：`limit` / `offset`，与仓库其它列表接口一致；
- 返回 `Page[ToolArtifactRead]`——**只返回索引，不返回文件内容**。

租户隔离不用自己写：`ToolArtifact` 是 `TenantScoped`
（`backend/app/models/entities.py:399`），ORM 事件会把租户条件注入所有 SELECT，
所以这个接口天然看不到别人的产物。

排序按 `created_at` 倒序——最新产出的排前面，符合"我刚跑完的东西在哪"的直觉。

### 3.2 前端：第 10 个视图「产物」

`frontend/src/panels.tsx` 的 `ArtifactPanel`：

- 左侧筛选：工具名、运行 ID（留空表示全部）；
- 右侧表格：路径 / 工具 / 大小 / 生成时间 / 产物 ID / 操作；
- **下载**：走 `frontend/src/api.ts` 的 `downloadArtifact`——
  下载接口要 `Authorization` 头，所以不能用普通 `<a href>`，
  得自己取 blob 再触发保存；
- **复制 ID**：产物 id 就是输入挂载参数（`file_id`）要填的值，
  复制按钮把这个闭环接上。

产物 ID 在表格里显示前 8 位（够区分），完整值放在 `title` 属性里，
并且用 `user-select:text` 保证**可以手动选中复制**——
剪贴板 API 在非安全上下文里可能不可用，不能把它当唯一入口。

## 4. 边界（不做什么）

- **不返回文件内容**。列表只有索引，内容一律走下载接口。
- **不做删除**。产物记录与文件都不提供删除入口：审计与产物同一思路，
  记录先于清理，清理交给运维侧的保留策略。
- **不做全文检索**。只按 `run_id` / `tool_name` 过滤，不对路径做模糊匹配——
  产物路径是容器内相对路径，模糊匹配的收益远小于误匹配的代价。
- **不开放跨租户查询**。没有 `tenant_id` 参数，也不做"管理员看所有租户"。
- **不改变下载语义**：仍然是 `application/octet-stream` + attachment，
  避免用户可控内容在 API origin 下被当 HTML 渲染。

## 5. 验证

- `backend/tests/test_executor.py` 新增 5 条：跨调用列出、两种过滤条件命中与不命中、
  分页边界（`limit=0` / `offset=-1` 返回 422）、**跨租户不可见**、下载头是 attachment。
- `frontend/e2e/smoke.py` 新增一步：切到「产物」视图，断言表头或空态出现。
  （本机没有 Docker，产物列表为空是正常的——这一步验证的是视图接线与接口联通。）

## 6. 与输入挂载的关系

两个特性是配套的：

```
产物中心（找到 id）──→ 工具参数 file_id ──→ 输入挂载（只读挂进容器）
```

没有产物中心，用户拿不到 id，输入挂载就只能由人手工从日志里翻——
那等于这个特性没有入口。

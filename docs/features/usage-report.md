# 用量报表

> 对标：Octop 的 `api/common/usage_xlsx.py` + `api/routers/usage`（用量统计 / 导出 xlsx）
> 状态：**已实现（运行次数与字符数口径，不含 token 与金额）**

## 1. 要解决什么

平台跑起来之后，"最近谁在用、用得多不多、哪个 Agent 最费"这些问题只能靠翻
运行记录表去数。Octop 有用量统计与 xlsx 导出，本项目补上同一层能力：
把运行与工具调用聚合成能直接看的报表。

## 2. 口径是什么（以及不是什么）

统计的是**运行次数与字符数**：

| 指标 | 来源 | 含义 |
|---|---|---|
| 运行次数 / 成功 / 失败 | `runs` 表 | 窗口内落库的运行记录 |
| 工具调用 | `runs.tool_calls` 或 `tool_invocations` | 按维度取其一，见下 |
| 输入 / 输出字符 | `runs.prompt_chars` / `answer_chars` | **不是 token** |
| 平均耗时 | `finished_at - started_at` | 只统计有结束时间的运行 |

**没有 token 计数**：模型网关目前不解析响应里的 `usage`，所以这个报表只能回答
「用得多不多」，回答不了「花了多少钱」。要接真实计费，得先让网关记录
prompt/completion tokens，再把字符数换成 token 数——接口形状不用变。

## 3. 为什么在 Python 里聚合

时间分桶在 SQLite 与 PostgreSQL 上用的函数不同（`date` / `date_trunc`），
时区处理也不同。放进 Python 聚合，两端行为一致，也才做得成
「没有数据的那天也要出现在折线图上」这种展示逻辑（`_bucket_keys`，
`backend/app/services/usage.py:87`）。

代价是窗口内的运行要取出来在内存里算，因此窗口上限 `MAX_DAYS = 366`
（`backend/app/services/usage.py:30`），且只取需要的几列，不加载整行实体。

## 4. 三个维度

- `agent`：按 Agent 分解，key 是 Agent id，label 是 Agent 名称；
- `model`：按模型分解（`runs.model`）；
- `tool`：按工具分解（`tool_invocations.tool_name`）。

**各维度的字段含义不完全一样**，这是有意的：

- `agent` / `model`：运行数与字符数都来自运行记录，工具调用取运行上累计的值；
- `tool`：「运行」是**用过该工具的运行数**（按 run_id 去重），「工具调用」是
  **该工具的调用次数**，字符数不适用（工具调用本身不产生 prompt/answer）。

另外，总量的工具调用数与按工具分解求和可能差几条：两者的时间过滤条件不同
（运行按开始时间、调用按调用时间），跨窗口边界的运行会落到不同的桶里。

## 5. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/usage/overview?days=30` | 总量指标 + 活跃维度数 + 日配额 |
| GET | `/usage/timeline?days=30&bucket=day` | 时间桶序列，空桶补零 |
| GET | `/usage/breakdown?days=30&dimension=agent` | 维度分解，按用量降序 |

`bucket` 取 `day` / `week` / `month`，`dimension` 取 `agent` / `model` / `tool`，
非法值返回 422。`days` 取值 1..366。

## 6. 前端

「用量」视图（侧栏第 9 项）：左边选时间范围 / 粒度 / 维度，右边是指标卡、
时间序列条形图与分解表。

条形图用原生 `<progress>` 画，不引图表库；时间序列按时间正序，
默认把图表滚到底部，否则「有数据的那天」在折叠线以下看不见。

## 7. 实现位置

| 关注点 | 位置 |
|---|---|
| 聚合逻辑 | `backend/app/services/usage.py` |
| 接口 | `backend/app/api/usage_routes.py` |
| DTO | `backend/app/schemas/dto.py`（`UsageOverviewRead` 等） |
| 前端 | `frontend/src/panels.tsx` `UsagePanel`、`frontend/src/App.tsx` 的「用量」视图 |

## 8. 测试

- 总量：运行/成功/失败/错误/工具调用/字符数/平均耗时；
- 活跃维度数：Agent 数、模型数、工具数；
- 窗口过滤：窗口外的运行不计入；
- 时间序列：空桶补零、按月分桶、非法 `bucket` 422；
- 维度分解：Agent 用名称而非 id、模型与工具各自的口径、非法 `dimension` 422；
- 边界与隔离：`days` 越界 422、跨租户不可见、viewer 角色可读。

## 9. 明确不做

- 不做 token 计数（网关不解析 `usage`）；
- 不做金额与单价换算（没有模型价格表）；
- 不做 xlsx 导出（Octop 有；先要能在界面看懂，再谈导出）；
- 不做实时推送（报表是打开时算的，不是订阅式的）；
- 不做跨租户汇总（报表只看本租户）。

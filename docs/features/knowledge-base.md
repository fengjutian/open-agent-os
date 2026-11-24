# 知识库（RAG）

> 对标：Octop 的 `infra/knowledge`（parse / chunk / embed / index / retrieve / citations / gate）
> 状态：**已实现（纯文本导入；向量为本地实现）**

## 1. 要解决什么

Agent 只知道自己上下文里的东西。要它回答"我们公司的报销流程是什么"，
得把文档喂进去、切成块、建索引，运行时按问题检索出相关片段再交给模型。

## 2. 链路

```text
建库 → 导入文档 → 切块 → 向量化 → 存块
提问 → 向量化查询 → 全量扫描当前库 → 余弦排序 → 取 top-k → 作为工具结果回灌模型
```

- **切块**：先按段落，过长再按句子，句子还长就硬切；块间保留 `overlap` 字符重叠，
  避免答案正好落在切口两侧。
- **检索**：当前库内先做**词面门控**（至少共享一个中文二元组/英文词，否则直接丢弃），
  再排序。排序默认按**词面覆盖率**（`overlap / len(query_features)`），可解释、不受
  哈希向量维度噪声影响；配 `KNOWLEDGE_RANKING=vector` 可切换成余弦相似度。
- **为什么加门控**：本地哈希向量在短文本上有可观的随机相似度——实测无关查询
  （"今天天气怎么样"）也能拿到 0.14 的余弦，不加门控就会把噪音当命中。

## 3. 向量用的是本地实现（重要）

`local-hash-v1`：中文按字符二元组、英文按词做特征，SHA-256 映射到 1024 维并归一化。

**它是词面相似度，不是语义相似度。** "预算超支"能命中"预算管理"，但"报销流程"
命中不了"费用核销步骤"。这是刻意的取舍——先不引入 embedding 服务依赖，
把接口、切块、检索、工具接入全部做通；要语义能力时实现一个
`EmbeddingProvider` 并在 `get_embedding_provider()` 里返回即可。

换实现的代价：**维度必须与已有向量一致**，否则要重建索引。每个库记录了
`embedding_model`，就是给这件事留的线索。

## 4. 模型怎么用

注册内置工具 `knowledge_search`（注册 + 策略放行，和别的工具一样）：

```json
{
  "name": "knowledge_search",
  "execution_type": "builtin",
  "input_schema": {
    "type": "object",
    "properties": {
      "knowledge_base_id": {"type": "string"},
      "query": {"type": "string"}
    },
    "required": ["knowledge_base_id", "query"],
    "additionalProperties": false
  }
}
```

返回 `{"count": n, "hits": [{document, seq, score, text}]}`，直接进 tool 消息。
和子代理一样，它需要数据库会话，所以走网关的专用分支。

## 5. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/knowledge-bases` | 建库 |
| GET | `/knowledge-bases` | 列表（含 `chunk_count` / `embedding_model`） |
| DELETE | `/knowledge-bases/{id}` | 删库（连带文档与块） |
| POST | `/knowledge-bases/{id}/documents` | 导入纯文本文档（自动切块+向量化） |
| GET | `/knowledge-bases/{id}/documents` | 文档列表 |
| DELETE | `/knowledge-bases/{id}/documents/{doc_id}` | 删文档（连带块） |
| POST | `/knowledge-bases/{id}/search` | 检索，返回 top-k 片段与分数 |

## 6. 前端界面

「知识库」视图（侧栏第 8 项）：

- 左侧建库、列出各库（显示块数与向量实现），点选切换；
- 右侧导入纯文本文档、列出文档与块数、就地检索并展示命中片段与分数；
- 写操作跟 Agent 同一档权限（`write:agents`），只读角色看到禁用态而不是按钮消失。

## 7. 边界与已知限制

- **只支持纯文本导入**：PDF/OCR/表格解析没有做（Octop 有 `parse.py`/`ocr.py`）；
- 检索是**全量扫描**，单库超过 `KNOWLEDGE_MAX_SCAN_CHUNKS`（默认 5000）时结果会被截断；
- 没有引用编号（citations）与来源门控（gate）；
- 没有后台索引任务队列（导入是同步的，大文档会让请求变慢）；
- 前端不提供 Agent 与知识库的绑定关系管理（工具参数里的 `knowledge_base_id` 由调用方给）。

## 8. 实现位置

| 关注点 | 位置 |
|---|---|
| 切块 | `backend/app/services/chunking.py` |
| 向量与余弦 | `backend/app/services/embeddings.py` |
| 建库/导入/检索 | `backend/app/services/knowledge.py` |
| 接口 | `backend/app/api/knowledge_routes.py` |
| 工具接入 | `backend/app/services/tool_gateway.py`（`knowledge_search` 分支） |
| 模型 | `KnowledgeBase` / `KnowledgeDocument` / `KnowledgeChunk` |
| 前端 | `frontend/src/panels.tsx` `KnowledgePanel`、`frontend/src/App.tsx` 的「知识库」视图 |

## 9. 测试

- 切块：长文本被切开且不超长、短文本原样、空文本返回空；
- 向量：同文本向量一致且归一化、相关文本余弦高于无关文本；
- API：建库/列表/导入（块数落库）/检索命中/无命中为空/删文档连块/删库/权限/跨租户/404；
- 工具：`knowledge_search` 经网关执行，检索结果进入 tool 消息；库不存在时返回空结果而不报错；
- 浏览器（`frontend/e2e/smoke.py`）：建库 → 导入文档 → 检索命中，走真实 UI。

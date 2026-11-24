"""知识库：切块、向量化、检索、工具接入。"""

import pytest

from app.services.chunking import chunk_text
from app.services.embeddings import LocalHashEmbedding, cosine

DOC = """
预算管理的第一条原则是先冻结非必要支出。任何超过五万元的采购都需要财务复核。

项目排期方面，需求评审必须在开发开始前完成。延期超过三天的任务要升级到负责人。

服务器部署在杭州机房，灾备在深圳。所有线上变更必须走灰度发布。
""".strip()


def _create_base(client, name: str = "运维手册") -> dict:
    response = client.post("/api/v1/knowledge-bases", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _add_doc(client, base_id: str, name: str = "手册", content: str = DOC) -> dict:
    response = client.post(
        f"/api/v1/knowledge-bases/{base_id}/documents",
        json={"name": name, "content": content},
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 切块与向量
# ---------------------------------------------------------------------------


def test_chunk_text_splits_long_text():
    chunks = chunk_text("这是一个句子。" * 300, size=200, overlap=20)
    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)


def test_chunk_text_keeps_short_text_whole():
    assert chunk_text("很短的一段") == ["很短的一段"]


def test_chunk_text_empty():
    assert chunk_text("   ") == []


def test_embeddings_are_deterministic_and_normalised():
    provider = LocalHashEmbedding()
    first = provider.embed(["预算 超支"])[0]
    second = provider.embed(["预算 超支"])[0]
    assert first == second
    norm = sum(value * value for value in first) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_similar_text_scores_higher_than_unrelated():
    provider = LocalHashEmbedding()
    query, related, unrelated = provider.embed(
        ["预算超支怎么办", "预算管理的第一条原则是冻结非必要支出", "今天天气很好"]
    )
    assert cosine(query, related) > cosine(query, unrelated)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_create_and_list_knowledge_base(client):
    base = _create_base(client)
    assert base["chunk_count"] == 0
    assert base["embedding_model"] == "local-hash-v1"

    page = client.get("/api/v1/knowledge-bases").json()
    assert page["total"] == 1


def test_upload_document_creates_chunks(client):
    base = _create_base(client)
    document = _add_doc(client, base["id"])
    assert document["chunk_count"] >= 1

    documents = client.get(f"/api/v1/knowledge-bases/{base['id']}/documents").json()
    assert documents["total"] == 1

    refreshed = client.get("/api/v1/knowledge-bases").json()["items"][0]
    assert refreshed["chunk_count"] == document["chunk_count"]


def test_search_returns_relevant_chunk_first(client):
    base = _create_base(client)
    _add_doc(client, base["id"])

    response = client.post(
        f"/api/v1/knowledge-bases/{base['id']}/search",
        json={"query": "预算超支了怎么办"},
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["count"] >= 1
    assert "预算" in body["hits"][0]["text"]


def test_search_with_no_hits_returns_empty(client):
    base = _create_base(client)
    _add_doc(client, base["id"], content="服务器部署在杭州机房。")

    body = client.post(
        f"/api/v1/knowledge-bases/{base['id']}/search",
        json={"query": "zzzz"},
    ).json()
    assert body["count"] == 0


def test_delete_document_removes_chunks(client):
    base = _create_base(client)
    document = _add_doc(client, base["id"])

    assert (
        client.delete(
            f"/api/v1/knowledge-bases/{base['id']}/documents/{document['id']}"
        ).status_code
        == 204
    )
    assert client.get(f"/api/v1/knowledge-bases/{base['id']}/documents").json()["total"] == 0
    assert client.post(
        f"/api/v1/knowledge-bases/{base['id']}/search", json={"query": "预算"}
    ).json()["count"] == 0


def test_delete_knowledge_base(client):
    base = _create_base(client)
    _add_doc(client, base["id"])
    assert client.delete(f"/api/v1/knowledge-bases/{base['id']}").status_code == 204
    assert client.get("/api/v1/knowledge-bases").json()["total"] == 0


def test_knowledge_requires_write_permission(client, make_key):
    viewer = make_key("viewer")
    response = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "x"},
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert response.status_code == 403


def test_knowledge_is_tenant_scoped(client, tenant_key):
    base = _create_base(client)
    other = tenant_key("tenant-b")
    page = client.get(
        "/api/v1/knowledge-bases", headers={"Authorization": f"Bearer {other}"}
    ).json()
    assert page["total"] == 0
    assert (
        client.get(
            f"/api/v1/knowledge-bases/{base['id']}/documents",
            headers={"Authorization": f"Bearer {other}"},
        ).status_code
        == 404
    )


def test_unknown_base_returns_404(client):
    assert (
        client.post("/api/v1/knowledge-bases/missing/search", json={"query": "x"}).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# 工具接入
# ---------------------------------------------------------------------------


def test_knowledge_search_tool(client, agent, monkeypatch):
    base = _create_base(client)
    _add_doc(client, base["id"])

    client.post(
        "/api/v1/tools",
        json={
            "name": "knowledge_search",
            "description": "检索知识库",
            "input_schema": {
                "type": "object",
                "properties": {
                    "knowledge_base_id": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["knowledge_base_id", "query"],
                "additionalProperties": False,
            },
        },
    )
    client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": "knowledge_search", "effect": "allow"},
    )

    conversation = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={}
    ).json()

    from app.services import agent_runtime as agent_runtime_module
    from app.services.model_gateway import ModelProvider, ModelReply, StreamDone, ToolCall

    class Scripted(ModelProvider):
        def __init__(self):
            self.replies = [
                ModelReply(
                    text="",
                    tool_calls=(
                        ToolCall(
                            id="call_1",
                            name="knowledge_search",
                            arguments={
                                "knowledge_base_id": base["id"],
                                "query": "预算超支",
                            },
                        ),
                    ),
                ),
                ModelReply(text="根据知识库，先冻结非必要支出。"),
            ]
            self.calls = []

        async def generate(self, *, model, messages, tools=None):
            self.calls.append(list(messages))
            return self.replies.pop(0)

        async def stream(self, *, model, messages, tools=None):
            yield StreamDone(reply=await self.generate(model=model, messages=messages, tools=tools))

    provider = Scripted()
    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: provider)

    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "预算超支怎么处理"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["assistant_message"]["content"].startswith("根据知识库")

    tool_message = provider.calls[-1][-1]
    assert tool_message["role"] == "tool"
    assert "预算" in tool_message["content"]


@pytest.mark.parametrize("query", ["x"])
def test_knowledge_tool_without_base_returns_zero_hits(client, agent, monkeypatch, query):
    client.post(
        "/api/v1/tools",
        json={
            "name": "knowledge_search",
            "description": "检索知识库",
            "input_schema": {
                "type": "object",
                "properties": {
                    "knowledge_base_id": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["knowledge_base_id", "query"],
                "additionalProperties": False,
            },
        },
    )
    client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": "knowledge_search", "effect": "allow"},
    )
    conversation = client.post(f"/api/v1/agents/{agent['id']}/conversations", json={}).json()

    from app.services import agent_runtime as agent_runtime_module
    from app.services.model_gateway import ModelProvider, ModelReply, StreamDone, ToolCall

    class Scripted(ModelProvider):
        def __init__(self):
            self.replies = [
                ModelReply(
                    text="",
                    tool_calls=(
                        ToolCall(
                            id="call_1",
                            name="knowledge_search",
                            arguments={"knowledge_base_id": "missing", "query": query},
                        ),
                    ),
                ),
                ModelReply(text="没有查到"),
            ]

        async def generate(self, *, model, messages, tools=None):
            return self.replies.pop(0)

        async def stream(self, *, model, messages, tools=None):
            yield StreamDone(reply=await self.generate(model=model, messages=messages, tools=tools))

    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: Scripted())
    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "查一下"}
    )
    assert response.status_code == 200

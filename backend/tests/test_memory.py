"""长期记忆：词法检索、注入、提炼。"""

import json

import pytest

from app.services import agent_runtime as agent_runtime_module
from app.services.memory import format_memory_block, score, tokens
from app.services.model_gateway import ModelProvider, ModelReply, StreamDone


class CapturingProvider(ModelProvider):
    def __init__(self, replies: list[ModelReply] | None = None) -> None:
        self._replies = list(replies or [])
        self.calls: list[list[dict]] = []

    async def generate(self, *, model, messages, tools=None):
        self.calls.append(list(messages))
        if self._replies:
            return self._replies.pop(0)
        return ModelReply(text="回答")

    async def stream(self, *, model, messages, tools=None):
        reply = await self.generate(model=model, messages=messages, tools=tools)
        yield StreamDone(reply=reply)

    @property
    def system_prompt(self) -> str:
        return str(self.calls[-1][0]["content"])


def _use(monkeypatch, provider) -> None:
    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: provider)


def _use_for_distill(monkeypatch, provider) -> None:
    """提炼走的是 memory_routes 里导入的 provider，需要单独打桩。"""
    import app.api.memory_routes as memory_routes_module

    monkeypatch.setattr(memory_routes_module, "get_model_provider", lambda: provider)


def _add_memory(client, content: str, **extra) -> dict:
    response = client.post("/api/v1/memories", json={"content": content, **extra})
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 词法打分
# ---------------------------------------------------------------------------


def test_tokens_split_chinese_and_ascii():
    parts = tokens("预算 CONTEXT_MAX 配置")
    assert "预算" in parts
    assert "context_max" in parts


def test_score_matches_related_text():
    query = tokens("主题偏好")
    assert score(query, "用户偏好深色主题") > 0
    assert score(query, "明天要下雨") == 0


def test_format_memory_block():
    class Item:
        def __init__(self, content: str) -> None:
            self.content = content

    block = format_memory_block([Item("喜欢深色主题"), Item("用中文回答")])
    assert block.startswith("[相关记忆]")
    assert "- 喜欢深色主题" in block


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_and_list_memory(client):
    created = _add_memory(client, "用户偏好深色主题", tags=["偏好"])
    assert created["tags"] == ["偏好"]

    page = client.get("/api/v1/memories").json()
    assert page["total"] == 1
    assert page["items"][0]["content"] == "用户偏好深色主题"


def test_memory_is_audited(client):
    _add_memory(client, "记一条")
    logs = client.get("/api/v1/audit-logs", params={"action": "memory.create"}).json()
    assert logs["total"] == 1


def test_list_memories_filters_by_query(client):
    _add_memory(client, "用户偏好深色主题")
    _add_memory(client, "服务器在杭州")

    page = client.get("/api/v1/memories", params={"q": "主题"}).json()
    assert page["total"] == 1
    assert "主题" in page["items"][0]["content"]


def test_delete_memory(client):
    created = _add_memory(client, "临时记忆")
    assert client.delete(f"/api/v1/memories/{created['id']}").status_code == 204
    assert client.get("/api/v1/memories").json()["total"] == 0


def test_memory_creation_requires_permission(client, make_key):
    viewer = make_key("viewer")
    response = client.post(
        "/api/v1/memories",
        json={"content": "x"},
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert response.status_code == 403


def test_memories_are_tenant_scoped(client, tenant_key):
    _add_memory(client, "默认租户的记忆")
    other = tenant_key("tenant-b")
    page = client.get("/api/v1/memories", headers={"Authorization": f"Bearer {other}"}).json()
    assert page["total"] == 0


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------


def test_related_memory_is_injected(client, agent, conversation, monkeypatch):
    _add_memory(client, "用户偏好深色主题", agent_id=agent["id"])
    provider = CapturingProvider()
    _use(monkeypatch, provider)

    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "我们用的主题是什么？"},
    )
    assert response.status_code == 200, response.text

    assert "[相关记忆]" in provider.system_prompt
    assert "用户偏好深色主题" in provider.system_prompt


def test_unrelated_memory_is_not_injected(client, agent, conversation, monkeypatch):
    _add_memory(client, "用户偏好深色主题", agent_id=agent["id"])
    provider = CapturingProvider()
    _use(monkeypatch, provider)

    client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "1 加 1 等于几"},
    )
    assert "[相关记忆]" not in provider.system_prompt


def test_memory_of_other_agent_is_not_injected(client, agent):
    other_agent = client.post(
        "/api/v1/agents", json={"name": "另一个", "model": "gpt-4o-mini"}
    ).json()
    conversation = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={}
    ).json()
    _add_memory(client, "这条属于另一个 Agent", agent_id=other_agent["id"])

    provider = CapturingProvider()
    agent_runtime_module.get_model_provider = lambda: provider  # type: ignore[assignment]
    try:
        client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={"content": "这条属于另一个 Agent 你知道吗"},
        )
        assert "[相关记忆]" not in provider.system_prompt
    finally:
        agent_runtime_module.get_model_provider = _ORIGINAL


_ORIGINAL = agent_runtime_module.get_model_provider


# ---------------------------------------------------------------------------
# 提炼
# ---------------------------------------------------------------------------


def test_distill_creates_memories(client, agent, conversation, monkeypatch):
    client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "我们用深色主题"}
    )
    payload = json.dumps(
        [{"content": "项目统一使用深色主题", "tags": ["偏好"]}], ensure_ascii=False
    )
    provider = CapturingProvider([ModelReply(text=payload)])
    _use_for_distill(monkeypatch, provider)

    response = client.post(f"/api/v1/conversations/{conversation['id']}/memories/distill")
    assert response.status_code == 200, response.text

    body = response.json()
    assert len(body) == 1
    assert body[0]["content"] == "项目统一使用深色主题"
    assert body[0]["source_conversation_id"] == conversation["id"]

    page = client.get("/api/v1/memories").json()
    assert page["total"] == 1


def test_distill_tolerates_non_json(client, agent, conversation, monkeypatch):
    client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "随便聊聊"}
    )
    provider = CapturingProvider([ModelReply(text="我觉得没什么好记的")])
    _use_for_distill(monkeypatch, provider)

    response = client.post(f"/api/v1/conversations/{conversation['id']}/memories/distill")
    assert response.status_code == 200
    assert response.json() == []


def test_distill_requires_enough_content(client, agent):
    # 全新会话没有任何消息
    empty = client.post(f"/api/v1/agents/{agent['id']}/conversations", json={}).json()
    response = client.post(f"/api/v1/conversations/{empty['id']}/memories/distill")
    assert response.status_code == 422


@pytest.mark.parametrize("limit", [1])
def test_distill_respects_limit(client, agent, conversation, monkeypatch, limit):
    client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "聊点东西"}
    )
    payload = json.dumps([{"content": f"记忆{i}", "tags": []} for i in range(5)], ensure_ascii=False)
    provider = CapturingProvider([ModelReply(text=payload)])
    _use_for_distill(monkeypatch, provider)

    settings_limit = limit
    from app.core.config import settings

    monkeypatch.setattr(settings, "memory_distill_limit", settings_limit)

    body = client.post(f"/api/v1/conversations/{conversation['id']}/memories/distill").json()
    assert len(body) == settings_limit

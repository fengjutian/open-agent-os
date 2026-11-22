"""Skill：CRUD、Agent 引用与运行时注入。"""

import pytest

from app.core.config import settings
from app.services import agent_runtime as agent_runtime_module
from app.services.model_gateway import ModelProvider, ModelReply, StreamDone


class RecordingProvider(ModelProvider):
    """记录每次调用收到的 messages，用来断言 system prompt 的内容。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def stream(self, *, model, messages, tools=None):
        self.calls.append(list(messages))
        yield StreamDone(reply=ModelReply(text="ok"))


def _skill(client, *, name="polite", instructions="回答时先打招呼。", **extra):
    return client.post(
        "/api/v1/skills",
        json={"name": name, "description": "测试技能", "instructions": instructions, **extra},
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_and_read_skill(client):
    response = _skill(client)
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["name"] == "polite"
    assert body["enabled"] is True

    fetched = client.get(f"/api/v1/skills/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["instructions"] == "回答时先打招呼。"


def test_list_skills(client):
    _skill(client, name="alpha")
    _skill(client, name="beta")

    page = client.get("/api/v1/skills").json()
    assert page["total"] == 2
    assert [item["name"] for item in page["items"]] == ["alpha", "beta"]


def test_duplicate_skill_name_conflicts(client):
    _skill(client, name="dup")
    assert _skill(client, name="dup").status_code == 409


@pytest.mark.parametrize("name", ["A", "1bad", "x", "has space"])
def test_invalid_skill_name_is_rejected(client, name):
    assert _skill(client, name=name).status_code == 422


def test_empty_instructions_rejected(client):
    assert _skill(client, instructions="").status_code == 422


def test_update_skill(client):
    created = _skill(client).json()

    response = client.patch(
        f"/api/v1/skills/{created['id']}",
        json={"enabled": False, "instructions": "新的指令"},
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["instructions"] == "新的指令"


def test_missing_skill_returns_404(client):
    assert client.get("/api/v1/skills/nope").status_code == 404


def test_skill_creation_is_audited(client):
    _skill(client)
    logs = client.get("/api/v1/audit-logs", params={"action": "skill.create"}).json()
    assert logs["total"] == 1
    assert logs["items"][0]["resource"] == "skill:polite"


# ---------------------------------------------------------------------------
# Agent 引用
# ---------------------------------------------------------------------------


def test_agent_can_reference_existing_skill(client):
    _skill(client, name="polite")

    agent = client.post("/api/v1/agents", json={"name": "a", "skill_names": ["polite"]})
    assert agent.status_code == 201, agent.text
    assert agent.json()["skill_names"] == ["polite"]


def test_agent_with_unknown_skill_is_rejected(client):
    response = client.post("/api/v1/agents", json={"name": "a", "skill_names": ["ghost"]})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["details"]["missing"] == ["ghost"]


def test_agent_update_validates_skills(client, agent):
    response = client.patch(f"/api/v1/agents/{agent['id']}", json={"skill_names": ["ghost"]})
    assert response.status_code == 422


def test_agent_update_rejects_null_skill_names(client, agent):
    response = client.patch(f"/api/v1/agents/{agent['id']}", json={"skill_names": None})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 运行时注入
# ---------------------------------------------------------------------------


def test_skill_instructions_are_injected(client, conversation, monkeypatch):
    _skill(client, name="polite", instructions="回答时先打招呼，并用中文。")
    client.patch(
        f"/api/v1/agents/{conversation['agent_id']}",
        json={"skill_names": ["polite"]},
    )

    provider = RecordingProvider()
    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: provider)

    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"}
    )
    assert response.status_code == 200

    system_message = provider.calls[0][0]
    assert system_message["role"] == "system"
    assert "## Skill: polite" in system_message["content"]
    assert "回答时先打招呼" in system_message["content"]


def test_applied_skills_are_recorded_in_run_events(client, conversation, monkeypatch):
    _skill(client, name="polite")
    client.patch(f"/api/v1/agents/{conversation['agent_id']}", json={"skill_names": ["polite"]})

    monkeypatch.setattr(agent_runtime_module, "get_model_provider", RecordingProvider)
    run_id = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"}
    ).json()["run_id"]

    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    started = next(event for event in events if event["type"] == "run.started")
    assert started["payload"]["skills"] == ["polite"]


def test_disabled_skill_is_skipped(client, conversation, monkeypatch):
    created = _skill(client, name="polite", instructions="不该出现的内容").json()
    client.patch(f"/api/v1/skills/{created['id']}", json={"enabled": False})
    client.patch(f"/api/v1/agents/{conversation['agent_id']}", json={"skill_names": ["polite"]})

    provider = RecordingProvider()
    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: provider)
    client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"})

    assert "不该出现的内容" not in provider.calls[0][0]["content"]


def test_skill_content_is_truncated_to_budget(client, conversation, monkeypatch):
    monkeypatch.setattr(settings, "skill_max_chars", 120)
    _skill(client, name="long", instructions="x" * 500)
    client.patch(f"/api/v1/agents/{conversation['agent_id']}", json={"skill_names": ["long"]})

    provider = RecordingProvider()
    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: provider)
    client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"})

    content = provider.calls[0][0]["content"]
    assert "技能内容已截断" in content
    assert len(content) < 500

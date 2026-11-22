"""Agent CRUD、校验与分页。"""

import pytest

from app.core.config import settings


def test_create_agent_fills_default_model_and_prompt(client):
    response = client.post("/api/v1/agents", json={"name": "a"})
    assert response.status_code == 201

    body = response.json()
    assert body["model"] == settings.model_name
    assert body["system_prompt"] == "You are a helpful assistant."
    assert body["description"] is None
    assert body["id"]


def test_create_agent_honours_explicit_model(client):
    body = client.post("/api/v1/agents", json={"name": "a", "model": "custom-model"}).json()
    assert body["model"] == "custom-model"


@pytest.mark.parametrize("name", ["", "x" * 121])
def test_create_agent_rejects_invalid_name(client, name):
    assert client.post("/api/v1/agents", json={"name": name}).status_code == 422


def test_get_agent(client, agent):
    response = client.get(f"/api/v1/agents/{agent['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == agent["id"]


def test_get_missing_agent_returns_error_envelope(client):
    response = client.get("/api/v1/agents/does-not-exist")
    assert response.status_code == 404

    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_list_agents_is_paginated(client):
    for index in range(3):
        client.post("/api/v1/agents", json={"name": f"agent-{index}"})

    first = client.get("/api/v1/agents", params={"limit": 2}).json()
    assert first["total"] == 3
    assert first["limit"] == 2
    assert first["offset"] == 0
    assert len(first["items"]) == 2

    second = client.get("/api/v1/agents", params={"limit": 2, "offset": 2}).json()
    assert len(second["items"]) == 1
    # 分页边界不能重叠
    assert not {a["id"] for a in first["items"]} & {a["id"] for a in second["items"]}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
def test_list_agents_rejects_out_of_range_paging(client, params):
    assert client.get("/api/v1/agents", params=params).status_code == 422


def test_update_agent(client, agent):
    response = client.patch(f"/api/v1/agents/{agent['id']}", json={"name": "renamed"})
    assert response.status_code == 200
    assert response.json()["name"] == "renamed"


def test_update_agent_ignores_unsent_fields(client, agent):
    response = client.patch(f"/api/v1/agents/{agent['id']}", json={})
    assert response.status_code == 200
    assert response.json()["name"] == agent["name"]


def test_update_agent_allows_clearing_description(client, agent):
    response = client.patch(f"/api/v1/agents/{agent['id']}", json={"description": None})
    assert response.status_code == 200
    assert response.json()["description"] is None


@pytest.mark.parametrize("field", ["name", "system_prompt", "model"])
def test_update_agent_rejects_explicit_null(client, agent, field):
    """回归：显式传 null 曾触发 NOT NULL 约束的裸 500。"""
    response = client.patch(f"/api/v1/agents/{agent['id']}", json={field: None})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_delete_agent(client, agent):
    assert client.delete(f"/api/v1/agents/{agent['id']}").status_code == 204
    assert client.get(f"/api/v1/agents/{agent['id']}").status_code == 404


def test_delete_agent_cascades_conversations_and_messages(client, agent, conversation):
    client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "hello"},
    )

    assert client.delete(f"/api/v1/agents/{agent['id']}").status_code == 204
    # 会话与消息应随 Agent 一起消失
    assert client.get(f"/api/v1/conversations/{conversation['id']}/messages").status_code == 404
    assert client.get(f"/api/v1/agents/{agent['id']}/conversations").status_code == 404


def test_delete_missing_agent_returns_404(client):
    assert client.delete("/api/v1/agents/nope").status_code == 404

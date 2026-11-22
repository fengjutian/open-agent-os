"""工具注册表与策略规则的 API 行为。"""

import pytest


def test_create_tool_persists_schema(client, echo_tool):
    assert echo_tool["name"] == "echo"
    assert echo_tool["execution_type"] == "builtin"
    assert echo_tool["enabled"] is True
    assert echo_tool["permissions"] == ["read:text"]
    assert echo_tool["input_schema"]["required"] == ["text"]


def test_duplicate_tool_name_conflicts(client, echo_tool):
    response = client.post("/api/v1/tools", json={"name": "echo"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_create_tool_rejects_invalid_json_schema(client):
    response = client.post(
        "/api/v1/tools",
        json={"name": "bad", "input_schema": {"type": "not-a-real-type"}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("name", ["Echo", "1echo", "e", "has space", "中文工具"])
def test_create_tool_rejects_bad_name(client, name):
    assert client.post("/api/v1/tools", json={"name": name}).status_code == 422


def test_tool_default_schema_accepts_no_arguments(client):
    tool = client.post("/api/v1/tools", json={"name": "noop"}).json()
    assert tool["input_schema"]["additionalProperties"] is False


def test_list_and_get_tools(client, echo_tool):
    page = client.get("/api/v1/tools").json()
    assert page["total"] == 1
    assert page["items"][0]["name"] == "echo"

    fetched = client.get(f"/api/v1/tools/{echo_tool['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == echo_tool["id"]


def test_get_missing_tool_returns_404(client):
    assert client.get("/api/v1/tools/nope").status_code == 404


def test_disable_tool(client, echo_tool):
    response = client.patch(f"/api/v1/tools/{echo_tool['id']}", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_tool_registration_is_audited(client, echo_tool):
    logs = client.get("/api/v1/audit-logs", params={"action": "tool.register"}).json()
    assert logs["total"] == 1
    assert logs["items"][0]["resource"] == "tool:echo"
    assert logs["items"][0]["actor"] == "system"


# ---------------------------------------------------------------------------
# 策略规则
# ---------------------------------------------------------------------------


def test_create_policy_rule(client, agent, echo_tool):
    response = client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": "echo", "effect": "allow"},
    )
    assert response.status_code == 201

    body = response.json()
    assert body["effect"] == "allow"
    assert body["priority"] == 100
    assert body["enabled"] is True


def test_policy_rule_for_missing_agent_returns_404(client):
    response = client.post(
        "/api/v1/policy-rules",
        json={"agent_id": "nope", "tool_name": "echo", "effect": "allow"},
    )
    assert response.status_code == 404


def test_policy_rule_rejects_unknown_effect(client, echo_tool):
    response = client.post(
        "/api/v1/policy-rules",
        json={"tool_name": "echo", "effect": "maybe"},
    )
    assert response.status_code == 422


def test_list_policy_rules_filters_by_agent(client, agent, allow_echo):
    page = client.get("/api/v1/policy-rules", params={"agent_id": agent["id"]}).json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == allow_echo["id"]

    assert client.get("/api/v1/policy-rules", params={"agent_id": "other"}).json()["total"] == 0


def test_policy_creation_is_audited(client, allow_echo):
    logs = client.get("/api/v1/audit-logs", params={"action": "policy.create"}).json()
    assert logs["total"] == 1
    assert logs["items"][0]["decision"] == "allow"


# ---------------------------------------------------------------------------
# Agent 视角的可用工具
# ---------------------------------------------------------------------------


def test_agent_tools_default_deny(client, agent, echo_tool):
    page = client.get(f"/api/v1/agents/{agent['id']}/tools").json()

    assert page["total"] == 1
    item = page["items"][0]
    assert item["enabled"] is True
    assert item["allowed"] is False
    assert "默认拒绝" in item["reason"]


def test_agent_tools_reflect_policy(client, agent, allow_echo):
    item = client.get(f"/api/v1/agents/{agent['id']}/tools").json()["items"][0]
    assert item["allowed"] is True


def test_agent_tools_show_disabled(client, agent, echo_tool):
    client.patch(f"/api/v1/tools/{echo_tool['id']}", json={"enabled": False})
    item = client.get(f"/api/v1/agents/{agent['id']}/tools").json()["items"][0]
    assert item["enabled"] is False
    assert item["allowed"] is False
    assert item["reason"] == "工具已停用"

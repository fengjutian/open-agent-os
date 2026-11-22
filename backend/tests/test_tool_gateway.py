"""受控调用网关：治理流水线的端到端行为。

覆盖顺序断言：未注册 → 参数不合法 → 策略拒绝 → 执行失败，每一步都留痕。
"""

import pytest


def _invoke(client, agent, tool_name, arguments, **extra):
    return client.post(
        f"/api/v1/agents/{agent['id']}/tools/{tool_name}/invoke",
        json={"arguments": arguments, **extra},
    )


def test_allowed_invocation_succeeds(client, agent, allow_echo):
    response = _invoke(client, agent, "echo", {"text": "hello"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["allowed"] is True
    assert body["result"] == {"text": "hello"}
    assert body["run_id"].startswith("run_")
    assert body["invocation_id"]


def test_invocation_records_conversation(client, agent, conversation, allow_echo):
    response = _invoke(
        client, agent, "echo", {"text": "hi"}, conversation_id=conversation["id"]
    )
    run_id = response.json()["run_id"]

    invocations = client.get("/api/v1/tool-invocations", params={"run_id": run_id}).json()
    assert invocations["total"] == 1
    assert invocations["items"][0]["conversation_id"] == conversation["id"]
    assert invocations["items"][0]["status"] == "succeeded"


def test_unregistered_tool_is_denied(client, agent):
    response = _invoke(client, agent, "nope", {})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "denied"
    assert body["allowed"] is False
    assert body["error_code"] == "tool_not_found"


def test_disabled_tool_is_denied(client, agent, allow_echo, echo_tool):
    client.patch(f"/api/v1/tools/{echo_tool['id']}", json={"enabled": False})

    body = _invoke(client, agent, "echo", {"text": "hi"}).json()
    assert body["status"] == "denied"
    assert body["error_code"] == "tool_disabled"


def test_arguments_must_match_schema(client, agent, allow_echo):
    body = _invoke(client, agent, "echo", {"text": 123}).json()

    assert body["status"] == "denied"
    assert body["error_code"] == "tool_arguments_invalid"


def test_missing_required_argument_is_denied(client, agent, allow_echo):
    body = _invoke(client, agent, "echo", {}).json()

    assert body["status"] == "denied"
    assert body["error_code"] == "tool_arguments_invalid"


def test_extra_argument_is_denied(client, agent, allow_echo):
    body = _invoke(client, agent, "echo", {"text": "hi", "extra": 1}).json()

    assert body["status"] == "denied"
    assert body["error_code"] == "tool_arguments_invalid"


def test_policy_deny_by_default(client, agent, echo_tool):
    """没有 allow 规则时，即使工具已注册且参数合法也必须拒绝。"""
    body = _invoke(client, agent, "echo", {"text": "hi"}).json()

    assert body["status"] == "denied"
    assert body["error_code"] == "policy_denied"


def test_explicit_deny_rule_wins(client, agent, allow_echo):
    client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": "echo", "effect": "deny", "priority": 200},
    )

    body = _invoke(client, agent, "echo", {"text": "hi"}).json()
    assert body["status"] == "denied"
    assert body["error_code"] == "policy_denied"


def test_executor_tool_fails_explicitly(client, agent):
    client.post("/api/v1/tools", json={"name": "run_code", "execution_type": "executor"})
    client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": "run_code", "effect": "allow"},
    )

    body = _invoke(client, agent, "run_code", {}).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "executor_not_available"


def test_registered_tool_without_implementation_cannot_run(client, agent):
    """安全性质：注册 ≠ 获得执行能力。"""
    client.post("/api/v1/tools", json={"name": "shell", "execution_type": "builtin"})
    client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": "shell", "effect": "allow"},
    )

    body = _invoke(client, agent, "shell", {}).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "tool_execution_failed"


def test_current_time_tool(client, agent):
    client.post("/api/v1/tools", json={"name": "current_time", "description": "当前时间"})
    client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": "current_time", "effect": "allow"},
    )

    body = _invoke(client, agent, "current_time", {}).json()
    assert body["status"] == "succeeded"
    assert "utc" in body["result"]


def test_denied_invocation_still_persisted_and_audited(client, agent):
    body = _invoke(client, agent, "echo", {"text": "hi"}).json()
    run_id = body["run_id"]

    # 被拒绝也要有调用记录
    invocations = client.get("/api/v1/tool-invocations", params={"run_id": run_id}).json()
    assert invocations["total"] == 1
    assert invocations["items"][0]["status"] == "denied"
    assert invocations["items"][0]["error_code"] == "tool_not_found"

    # 且审计里有 deny 决策
    audits = client.get("/api/v1/audit-logs", params={"run_id": run_id}).json()
    assert audits["total"] >= 1
    assert audits["items"][0]["decision"] == "deny"
    assert audits["items"][0]["action"] == "tool.invoke"


def test_allowed_invocation_is_audited_before_execution(client, agent, allow_echo):
    run_id = _invoke(client, agent, "echo", {"text": "hi"}).json()["run_id"]

    audits = client.get("/api/v1/audit-logs", params={"run_id": run_id}).json()
    assert audits["total"] == 1
    entry = audits["items"][0]
    assert entry["decision"] == "allow"
    assert entry["resource"] == "tool:echo"
    assert entry["actor"] == f"agent:{agent['id']}"
    # 审计只记参数名，不记参数值
    assert entry["detail"]["argument_keys"] == ["text"]
    assert "hi" not in str(entry["detail"])


def test_failed_execution_adds_second_audit_event(client, agent):
    client.post("/api/v1/tools", json={"name": "shell", "execution_type": "builtin"})
    client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": "shell", "effect": "allow"},
    )

    run_id = _invoke(client, agent, "shell", {}).json()["run_id"]

    audits = client.get("/api/v1/audit-logs", params={"run_id": run_id}).json()
    actions = {entry["action"] for entry in audits["items"]}
    assert actions == {"tool.invoke", "tool.execute.failed"}


def test_invoke_for_missing_agent_returns_404(client):
    response = client.post(
        "/api/v1/agents/nope/tools/echo/invoke", json={"arguments": {"text": "hi"}}
    )
    assert response.status_code == 404


def test_invoke_with_missing_conversation_returns_404(client, agent, allow_echo):
    response = _invoke(client, agent, "echo", {"text": "hi"}, conversation_id="nope")
    assert response.status_code == 404


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
def test_invocation_list_validates_paging(client, params):
    assert client.get("/api/v1/tool-invocations", params=params).status_code == 422

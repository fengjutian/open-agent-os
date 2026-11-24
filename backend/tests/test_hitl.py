"""HITL：工具调用前暂停、人工批准/拒绝、从检查点续跑。"""

import json

import pytest

from app.db.session import SessionLocal
from app.models.entities import Run, ToolApproval, ToolInvocation
from app.services import agent_runtime as agent_runtime_module
from app.services.model_gateway import ModelProvider, ModelReply, StreamDone, ToolCall

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class ScriptedProvider(ModelProvider):
    """按脚本依次返回；记录模型看到的 messages 以便断言回灌内容。"""

    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    async def generate(self, *, model, messages, tools=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._replies:
            raise AssertionError("provider 调用次数超出脚本预期")
        return self._replies.pop(0)

    async def stream(self, *, model, messages, tools=None):
        reply = await self.generate(model=model, messages=messages, tools=tools)
        yield StreamDone(reply=reply)


def _use(monkeypatch, provider) -> None:
    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: provider)


def _tool_call(text: str = "hi") -> ModelReply:
    return ModelReply(
        text="", tool_calls=(ToolCall(id="call_1", name="echo", arguments={"text": text}),)
    )


def _allow(client, agent, tool_name="echo"):
    response = client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": tool_name, "effect": "allow"},
    )
    assert response.status_code == 201, response.text


@pytest.fixture
def approval_tool(client, agent):
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "echo",
            "description": "需要人工审批的回显",
            "input_schema": ECHO_SCHEMA,
            "requires_approval": True,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["requires_approval"] is True
    _allow(client, agent)
    return response.json()


def _send(client, conversation_id: str, text: str = "帮我回显"):
    return client.post(f"/api/v1/conversations/{conversation_id}/messages", json={"content": text})


# ---------------------------------------------------------------------------
# 暂停
# ---------------------------------------------------------------------------


def test_requires_approval_pauses_run(client, agent, conversation, approval_tool, monkeypatch):
    _use(monkeypatch, ScriptedProvider([_tool_call(), ModelReply(text="完成")]))

    response = _send(client, conversation["id"])
    assert response.status_code == 202, response.text

    body = response.json()
    assert body["status"] == "waiting_approval"
    assert body["tool"] == "echo"
    assert body["approval_id"]

    with SessionLocal() as db:
        run = db.get(Run, body["run_id"])
        assert run.status == "waiting_approval"
        assert run.checkpoint, "暂停时必须留下检查点"
        approval = db.get(ToolApproval, body["approval_id"])
        assert approval.status == "pending"
        assert approval.arguments == {"text": "hi"}
        assert approval.call_id == "call_1"

        # 工具没有被执行
        invocation = db.get(ToolInvocation, approval.invocation_id)
        assert invocation.status == "pending_approval"


def test_approval_is_listed(client, agent, conversation, approval_tool, monkeypatch):
    _use(monkeypatch, ScriptedProvider([_tool_call(), ModelReply(text="完成")]))
    created = _send(client, conversation["id"]).json()

    page = client.get("/api/v1/approvals").json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == created["approval_id"]
    assert page["items"][0]["tool_name"] == "echo"

    # 已处理的不再出现在默认列表里
    client.post(f"/api/v1/approvals/{created['approval_id']}/approve", json={})
    assert client.get("/api/v1/approvals").json()["total"] == 0
    assert client.get("/api/v1/approvals", params={"status": "approved"}).json()["total"] == 1


# ---------------------------------------------------------------------------
# 批准
# ---------------------------------------------------------------------------


def test_approve_executes_tool_and_resumes_run(
    client, agent, conversation, approval_tool, monkeypatch
):
    provider = ScriptedProvider([_tool_call(), ModelReply(text="完成")])
    _use(monkeypatch, provider)
    created = _send(client, conversation["id"]).json()

    response = client.post(f"/api/v1/approvals/{created['approval_id']}/approve", json={})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["assistant_message"]["content"] == "完成"

    with SessionLocal() as db:
        run = db.get(Run, created["run_id"])
        assert run.status == "succeeded"
        approval = db.get(ToolApproval, created["approval_id"])
        assert approval.status == "approved"
        assert approval.decided_by.startswith("key:")
        invocation = db.get(ToolInvocation, approval.invocation_id)
        assert invocation.status == "succeeded"

    # 模型第二轮看到的是工具执行结果
    tool_message = provider.calls[-1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "succeeded" in tool_message["content"]

    # 审计留痕
    logs = client.get("/api/v1/audit-logs", params={"action": "tool.approval.approved"}).json()
    assert logs["total"] == 1


def test_approval_cannot_be_decided_twice(
    client, agent, conversation, approval_tool, monkeypatch
):
    _use(monkeypatch, ScriptedProvider([_tool_call(), ModelReply(text="完成")]))
    created = _send(client, conversation["id"]).json()

    assert client.post(f"/api/v1/approvals/{created['approval_id']}/approve", json={}).status_code == 200
    again = client.post(f"/api/v1/approvals/{created['approval_id']}/approve", json={})
    assert again.status_code == 409


def test_unknown_approval_returns_404(client):
    assert client.post("/api/v1/approvals/missing/approve", json={}).status_code == 404


# ---------------------------------------------------------------------------
# 拒绝
# ---------------------------------------------------------------------------


def test_reject_skips_tool_and_lets_model_finish(
    client, agent, conversation, approval_tool, monkeypatch
):
    provider = ScriptedProvider([_tool_call(), ModelReply(text="好的，不调用工具")])
    _use(monkeypatch, provider)
    created = _send(client, conversation["id"]).json()

    response = client.post(
        f"/api/v1/approvals/{created['approval_id']}/reject", json={"reason": "这次不要调用"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["assistant_message"]["content"] == "好的，不调用工具"

    with SessionLocal() as db:
        run = db.get(Run, created["run_id"])
        assert run.status == "succeeded"
        approval = db.get(ToolApproval, created["approval_id"])
        assert approval.status == "rejected"
        assert approval.reason == "这次不要调用"
        invocation = db.get(ToolInvocation, approval.invocation_id)
        # 工具始终没有被执行
        assert invocation.status == "pending_approval"

    # 模型看到「被拒绝」，而不是成功结果
    tool_message = provider.calls[-1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "rejected" in tool_message["content"]
    assert "这次不要调用" in tool_message["content"]


def test_reject_requires_reason_only_optionally(
    client, agent, conversation, approval_tool, monkeypatch
):
    _use(monkeypatch, ScriptedProvider([_tool_call(), ModelReply(text="ok")]))
    created = _send(client, conversation["id"]).json()
    assert (
        client.post(f"/api/v1/approvals/{created['approval_id']}/reject", json={}).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# 流式
# ---------------------------------------------------------------------------


def test_stream_pauses_with_event(client, agent, conversation, approval_tool, monkeypatch):
    _use(monkeypatch, ScriptedProvider([_tool_call(), ModelReply(text="完成")]))

    events: list[dict] = []
    with client.stream(
        "POST",
        f"/api/v1/conversations/{conversation['id']}/messages/stream",
        json={"content": "帮我回显"},
    ) as response:
        name = None
        for raw in response.iter_lines():
            if raw.startswith("event:"):
                name = raw[6:].strip()
            elif raw.startswith("data:"):
                events.append({"type": name, **json.loads(raw[5:].strip())})

    types = [event["type"] for event in events]
    assert "run.waiting_approval" in types
    paused = next(event for event in events if event["type"] == "run.waiting_approval")
    assert paused["approval_id"]

    with SessionLocal() as db:
        assert db.get(Run, paused["run_id"]).status == "waiting_approval"


# ---------------------------------------------------------------------------
# 权限与租户
# ---------------------------------------------------------------------------


def test_approve_requires_write_permission(
    client, agent, conversation, approval_tool, monkeypatch, make_key
):
    _use(monkeypatch, ScriptedProvider([_tool_call(), ModelReply(text="完成")]))
    created = _send(client, conversation["id"]).json()

    viewer = make_key("viewer")
    response = client.post(
        f"/api/v1/approvals/{created['approval_id']}/approve",
        json={},
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert response.status_code == 403


def test_approvals_are_tenant_scoped(
    client, agent, conversation, approval_tool, monkeypatch, tenant_key
):
    _use(monkeypatch, ScriptedProvider([_tool_call(), ModelReply(text="完成")]))
    created = _send(client, conversation["id"]).json()

    other = tenant_key("tenant-b")
    assert (
        client.get(
            "/api/v1/approvals", headers={"Authorization": f"Bearer {other}"}
        ).json()["total"]
        == 0
    )
    assert (
        client.post(
            f"/api/v1/approvals/{created['approval_id']}/approve",
            json={},
            headers={"Authorization": f"Bearer {other}"},
        ).status_code
        == 404
    )

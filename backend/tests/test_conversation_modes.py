"""会话模式 Ask / Plan / Craft。

语义对齐 Octop：ask 只放只读工具、plan 只读并要求先出计划、craft 全开，
未知模式回落 craft。
"""

import asyncio

import pytest

from app.db.session import SessionLocal
from app.models.entities import Conversation
from app.services.agent_runtime import AgentRuntime
from app.services.conversation_modes import filter_tool_specs, mode_hint, normalize_mode
from app.services.model_gateway import ModelReply, StreamDone


class CapturingProvider:
    """记录模型实际看到的工具与系统提示。"""

    def __init__(self, text: str = "ok") -> None:
        self.text = text
        self.tools: list[str] = []
        self.system = ""

    def _capture(self, messages, tools) -> None:
        self.tools = [spec["function"]["name"] for spec in (tools or [])]
        self.system = messages[0]["content"] if messages else ""

    async def generate(self, *, model, messages, tools=None):
        self._capture(messages, tools)
        return ModelReply(text=self.text, tool_calls=())

    async def stream(self, *, model, messages, tools=None):
        self._capture(messages, tools)
        yield StreamDone(reply=ModelReply(text=self.text, tool_calls=()))


def run_agent(conversation_id: str, *, mode: str | None = None) -> CapturingProvider:
    provider = CapturingProvider()
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        asyncio.run(AgentRuntime(provider=provider).respond(db, conversation, "hi", mode=mode))
    return provider


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("ask", "ask"), ("plan", "plan"), ("craft", "craft"), (None, "craft"), ("bogus", "craft")],
)
def test_normalize_mode(value, expected):
    assert normalize_mode(value) == expected


def test_mode_hints():
    assert "Ask mode" in mode_hint("ask")
    assert "Plan mode" in mode_hint("plan")
    assert mode_hint("craft") == ""


def _spec(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_filter_tool_specs_by_mode():
    specs = [_spec("echo"), _spec("current_time"), _spec("mcp_search")]

    assert len(filter_tool_specs(specs, mode="craft")) == 3
    # 只读白名单里有 current_time；echo 与未知 MCP 工具在 ask 下不可见
    assert [s["function"]["name"] for s in filter_tool_specs(specs, mode="ask")] == ["current_time"]
    assert [s["function"]["name"] for s in filter_tool_specs(specs, mode="plan")] == ["current_time"]


def test_filter_tool_specs_honours_read_only_flag():
    specs = [_spec("echo"), _spec("mcp_search")]
    kept = filter_tool_specs(specs, mode="ask", read_only_names={"mcp_search"})
    assert [s["function"]["name"] for s in kept] == ["mcp_search"]


# ---------------------------------------------------------------------------
# 运行时：工具可见性与提示词
# ---------------------------------------------------------------------------


def test_craft_mode_sees_all_allowed_tools(client, agent, conversation, allow_echo):
    provider = run_agent(conversation["id"], mode="craft")

    assert "echo" in provider.tools
    assert "Ask mode" not in provider.system


def test_ask_mode_hides_side_effect_tools(client, agent, conversation, allow_echo):
    provider = run_agent(conversation["id"], mode="ask")

    assert "echo" not in provider.tools
    assert "Ask mode" in provider.system


def test_plan_mode_hides_tools_and_asks_for_a_plan(client, agent, conversation, allow_echo):
    provider = run_agent(conversation["id"], mode="plan")

    assert "echo" not in provider.tools
    assert "Plan mode" in provider.system
    assert "confirm" in provider.system


def test_read_only_tool_survives_ask_mode(client, agent, conversation):
    # 注册时显式声明只读 → ask 模式下仍然可见
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "lookup",
            "description": "只读查询",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "read_only": True,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["read_only"] is True

    client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": "lookup", "effect": "allow"},
    )

    provider = run_agent(conversation["id"], mode="ask")
    assert "lookup" in provider.tools


def test_conversation_mode_is_sticky(client, agent, conversation):
    with SessionLocal() as db:
        row = db.get(Conversation, conversation["id"])
        row.mode = "ask"
        db.commit()

    # 不传 mode：应沿用会话上的 ask
    provider = run_agent(conversation["id"])
    assert "Ask mode" in provider.system
    assert "echo" not in provider.tools


def test_request_mode_does_not_change_sticky_mode(client, agent, conversation, allow_echo):
    provider = run_agent(conversation["id"], mode="ask")
    assert "echo" not in provider.tools

    with SessionLocal() as db:
        assert db.get(Conversation, conversation["id"]).mode == "craft"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_create_conversation_with_mode(client, agent):
    response = client.post(
        f"/api/v1/agents/{agent['id']}/conversations",
        json={"title": "计划", "mode": "plan"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["mode"] == "plan"


def test_conversation_defaults_to_craft(client, agent):
    response = client.post(f"/api/v1/agents/{agent['id']}/conversations", json={})
    assert response.json()["mode"] == "craft"


def test_invalid_mode_is_rejected(client, agent, conversation):
    assert (
        client.post(
            f"/api/v1/agents/{agent['id']}/conversations", json={"mode": "turbo"}
        ).status_code
        == 422
    )
    assert client.patch(f"/api/v1/conversations/{conversation['id']}", json={"mode": "turbo"}).status_code == 422


def test_patch_conversation_mode(client, conversation):
    response = client.patch(
        f"/api/v1/conversations/{conversation['id']}", json={"mode": "ask"}
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "ask"

    listed = client.get(f"/api/v1/agents/{conversation['agent_id']}/conversations").json()
    assert listed["items"][0]["mode"] == "ask"


def test_patch_conversation_rejects_explicit_null(client, conversation):
    response = client.patch(f"/api/v1/conversations/{conversation['id']}", json={"mode": None})
    assert response.status_code == 422


def test_patch_missing_conversation_returns_404(client):
    assert client.patch("/api/v1/conversations/missing", json={"mode": "ask"}).status_code == 404


def test_reserved_mode_is_recorded_in_run_started(client, agent, conversation):
    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "hi", "mode": "ask"},
    )
    assert response.status_code == 200, response.text

    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    started = next(event for event in events if event["type"] == "run.started")
    assert started["payload"]["mode"] == "ask"

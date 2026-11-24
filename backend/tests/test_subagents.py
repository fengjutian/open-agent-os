"""子代理：把子任务交给另一个 Agent 执行。"""

import json

import pytest

from app.db.session import SessionLocal
from app.models.entities import Conversation, Run
from app.services import agent_runtime as agent_runtime_module
from app.services.model_gateway import ModelProvider, ModelReply, StreamDone, ToolCall

SPAWN_SCHEMA = {
    "type": "object",
    "properties": {
        "agent_id": {"type": "string"},
        "task": {"type": "string"},
    },
    "required": ["task"],
    "additionalProperties": False,
}


class ScriptedProvider(ModelProvider):
    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    async def generate(self, *, model, messages, tools=None):
        self.calls.append(list(messages))
        if not self._replies:
            raise AssertionError("provider 调用次数超出脚本预期")
        return self._replies.pop(0)

    async def stream(self, *, model, messages, tools=None):
        reply = await self.generate(model=model, messages=messages, tools=tools)
        yield StreamDone(reply=reply)


def _use(monkeypatch, provider) -> None:
    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: provider)


def _spawn_agent(client, name: str) -> dict:
    response = client.post(
        "/api/v1/agents", json={"name": name, "model": "gpt-4o-mini"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _register_spawn_tool(client) -> dict:
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "spawn_subagent",
            "description": "把子任务交给另一个 Agent",
            "input_schema": SPAWN_SCHEMA,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _allow(client, agent_id: str, tool_name: str = "spawn_subagent") -> None:
    response = client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent_id, "tool_name": tool_name, "effect": "allow"},
    )
    assert response.status_code == 201, response.text


def _delegate_call(agent_id: str, task: str, call_id: str = "call_1") -> ModelReply:
    return ModelReply(
        text="",
        tool_calls=(
            ToolCall(
                id=call_id,
                name="spawn_subagent",
                arguments={"agent_id": agent_id, "task": task},
            ),
        ),
    )


def _send(client, conversation_id: str, text: str = "安排一下"):
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages", json={"content": text}
    )


# ---------------------------------------------------------------------------
# 正常委派
# ---------------------------------------------------------------------------


def test_subagent_runs_child_and_returns_answer(
    client, agent, conversation, monkeypatch
):
    child = _spawn_agent(client, "子代理")
    _register_spawn_tool(client)
    _allow(client, agent["id"])

    provider = ScriptedProvider(
        [
            _delegate_call(child["id"], "统计一下用户数"),  # 主：发起委派
            ModelReply(text="用户数是 42"),  # 子：给出答案
            ModelReply(text="子代理说用户数是 42"),  # 主：汇总
        ]
    )
    _use(monkeypatch, provider)

    response = _send(client, conversation["id"])
    assert response.status_code == 200, response.text
    assert response.json()["assistant_message"]["content"] == "子代理说用户数是 42"

    with SessionLocal() as db:
        # 子代理有自己的会话与运行记录
        children = db.query(Conversation).filter(Conversation.agent_id == child["id"]).all()
        assert len(children) == 1
        assert children[0].title.startswith("子任务：")

        runs = db.query(Run).all()
        assert len(runs) == 2
        assert len({run.id for run in runs}) == 2, "父子运行不能共用 run_id"


def test_subagent_result_is_fed_back_to_main_model(
    client, agent, conversation, monkeypatch
):
    child = _spawn_agent(client, "子代理")
    _register_spawn_tool(client)
    _allow(client, agent["id"])

    provider = ScriptedProvider(
        [
            _delegate_call(child["id"], "查一下库存"),
            ModelReply(text="库存 7 件"),
            ModelReply(text="好的"),
        ]
    )
    _use(monkeypatch, provider)

    _send(client, conversation["id"])

    # 主模型第二次调用时，最后一条应是子代理返回的工具结果
    final_call = provider.calls[-1]
    tool_message = final_call[-1]
    assert tool_message["role"] == "tool"
    payload = json.loads(tool_message["content"])
    assert payload["status"] == "succeeded"
    assert payload["result"]["answer"] == "库存 7 件"


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_missing_target_agent_returns_failed_outcome(
    client, agent, conversation, monkeypatch
):
    _register_spawn_tool(client)
    _allow(client, agent["id"])

    provider = ScriptedProvider(
        [
            _delegate_call("missing-agent", "干点什么"),
            ModelReply(text="没找到那个 Agent"),
        ]
    )
    _use(monkeypatch, provider)

    response = _send(client, conversation["id"])
    assert response.status_code == 200
    assert response.json()["assistant_message"]["content"] == "没找到那个 Agent"


def test_empty_task_is_rejected(client, agent, conversation, monkeypatch):
    child = _spawn_agent(client, "子代理")
    _register_spawn_tool(client)
    _allow(client, agent["id"])

    provider = ScriptedProvider(
        [_delegate_call(child["id"], "   "), ModelReply(text="任务为空")]
    )
    _use(monkeypatch, provider)

    assert _send(client, conversation["id"]).status_code == 200


def test_depth_limit_stops_recursion(client, agent, monkeypatch):
    """A 派 B、B 再派 B：第二层应被拒绝，而不是无限递归。"""
    child = _spawn_agent(client, "子代理")
    _register_spawn_tool(client)
    _allow(client, agent["id"])
    _allow(client, child["id"])

    # 主 → 子 → 子 → （第三层被拒）→ 逐层收尾
    provider = ScriptedProvider(
        [
            _delegate_call(child["id"], "第一层"),
            _delegate_call(child["id"], "第二层"),
            ModelReply(text="第二层回答"),
            ModelReply(text="第一层回答"),
            ModelReply(text="全部完成"),
        ]
    )
    _use(monkeypatch, provider)

    conversation = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={}
    ).json()
    response = _send(client, conversation["id"])
    assert response.status_code == 200, response.text

    from app.core.config import settings

    # 默认上限 2：主(0) → 子(1) → 子(2) 被拒
    assert settings.subagent_max_depth == 2


@pytest.mark.parametrize("depth", [1])
def test_configurable_depth(client, agent, monkeypatch, depth):
    from app.core.config import settings

    monkeypatch.setattr(settings, "subagent_max_depth", depth)
    child = _spawn_agent(client, "子代理")
    _register_spawn_tool(client)
    _allow(client, agent["id"])
    _allow(client, child["id"])

    provider = ScriptedProvider(
        [
            _delegate_call(child["id"], "第一层"),
            _delegate_call(child["id"], "第二层"),
            ModelReply(text="结束"),
        ]
    )
    _use(monkeypatch, provider)

    conversation = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={}
    ).json()
    assert _send(client, conversation["id"]).status_code == 200

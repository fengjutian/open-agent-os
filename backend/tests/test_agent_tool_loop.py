"""Agent Loop 与工具治理的集成行为。"""

import json

from app.core.config import settings
from app.services import agent_runtime as agent_runtime_module
from app.services.agent_runtime import max_iterations_message
from app.services.model_gateway import ModelProvider, ModelReply, ToolCall


class ScriptedProvider(ModelProvider):
    """按脚本依次返回回复，并记录每次调用的入参。"""

    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    async def generate(self, *, model, messages, tools=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._replies:
            raise AssertionError("provider 调用次数超出脚本预期")
        return self._replies.pop(0)


def _use(monkeypatch, provider: ScriptedProvider) -> None:
    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: provider)


def _echo_call(text: str, call_id: str = "call_1") -> ModelReply:
    return ModelReply(
        text="",
        tool_calls=(ToolCall(id=call_id, name="echo", arguments={"text": text}),),
    )


def _send(client, conversation, text: str = "帮我回显一下"):
    return client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": text},
    )


def test_only_allowed_tools_are_advertised_to_the_model(
    client, conversation, allow_echo, monkeypatch
):
    provider = ScriptedProvider([ModelReply(text="好的")])
    _use(monkeypatch, provider)

    _send(client, conversation)

    advertised = provider.calls[0]["tools"]
    assert [spec["function"]["name"] for spec in advertised] == ["echo"]


def test_no_tool_is_advertised_when_policy_denies_all(client, conversation, echo_tool, monkeypatch):
    """工具已注册但没放行：模型连看都看不到它。"""
    provider = ScriptedProvider([ModelReply(text="好的")])
    _use(monkeypatch, provider)

    _send(client, conversation)

    assert provider.calls[0]["tools"] is None


def test_tool_call_is_executed_then_final_answer(
    client, conversation, allow_echo, monkeypatch
):
    provider = ScriptedProvider([_echo_call("hi"), ModelReply(text="最终答案")])
    _use(monkeypatch, provider)

    response = _send(client, conversation)
    assert response.status_code == 200

    body = response.json()
    assert body["assistant_message"]["content"] == "最终答案"

    invocations = client.get(
        "/api/v1/tool-invocations", params={"run_id": body["run_id"]}
    ).json()
    assert invocations["total"] == 1
    record = invocations["items"][0]
    assert record["status"] == "succeeded"
    assert record["conversation_id"] == conversation["id"]
    assert record["result"] == {"text": "hi"}


def test_tool_result_is_fed_back_to_the_model(client, conversation, allow_echo, monkeypatch):
    provider = ScriptedProvider([_echo_call("hi"), ModelReply(text="最终答案")])
    _use(monkeypatch, provider)

    _send(client, conversation)

    second_call_messages = provider.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m["role"] == "tool"]
    assert len(tool_messages) == 1

    payload = json.loads(tool_messages[0]["content"])
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert payload["status"] == "succeeded"
    assert payload["result"] == {"text": "hi"}


def test_denied_tool_call_is_reported_back_not_executed(
    client, conversation, echo_tool, monkeypatch
):
    """模型请求一个未被放行的工具：拒绝原因要回灌给模型，而不是假装成功。"""
    provider = ScriptedProvider([_echo_call("hi"), ModelReply(text="我无法调用该工具")])
    _use(monkeypatch, provider)

    response = _send(client, conversation)
    body = response.json()
    assert body["assistant_message"]["content"] == "我无法调用该工具"

    invocations = client.get(
        "/api/v1/tool-invocations", params={"run_id": body["run_id"]}
    ).json()
    assert invocations["items"][0]["status"] == "denied"
    assert invocations["items"][0]["error_code"] == "policy_denied"

    tool_message = [
        m for m in provider.calls[1]["messages"] if m["role"] == "tool"
    ][0]
    payload = json.loads(tool_message["content"])
    assert payload["status"] == "denied"
    assert payload["error"]["code"] == "policy_denied"


def test_invalid_arguments_from_model_are_denied(client, conversation, allow_echo, monkeypatch):
    bad_call = ModelReply(
        text="",
        tool_calls=(ToolCall(id="call_1", name="echo", arguments={"text": 123}),),
    )
    provider = ScriptedProvider([bad_call, ModelReply(text="参数不对，我换个方式")])
    _use(monkeypatch, provider)

    body = _send(client, conversation).json()
    invocations = client.get(
        "/api/v1/tool-invocations", params={"run_id": body["run_id"]}
    ).json()
    assert invocations["items"][0]["status"] == "denied"
    assert invocations["items"][0]["error_code"] == "tool_arguments_invalid"


def test_unknown_tool_request_is_denied(client, conversation, monkeypatch):
    hallucinated = ModelReply(
        text="",
        tool_calls=(ToolCall(id="call_1", name="rm_rf", arguments={}),),
    )
    provider = ScriptedProvider([hallucinated, ModelReply(text="没有这个工具")])
    _use(monkeypatch, provider)

    body = _send(client, conversation).json()
    invocations = client.get(
        "/api/v1/tool-invocations", params={"run_id": body["run_id"]}
    ).json()
    assert invocations["items"][0]["error_code"] == "tool_not_found"


def test_loop_stops_at_max_iterations(client, conversation, allow_echo, monkeypatch):
    limit = settings.max_tool_iterations
    provider = ScriptedProvider([_echo_call("again") for _ in range(limit + 2)])
    _use(monkeypatch, provider)

    body = _send(client, conversation).json()

    assert body["assistant_message"]["content"] == max_iterations_message(limit)
    assert len(provider.calls) == limit

    invocations = client.get(
        "/api/v1/tool-invocations", params={"run_id": body["run_id"]}
    ).json()
    assert invocations["total"] == limit

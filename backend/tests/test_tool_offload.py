"""大 payload 旁路：结果剥离、artifact.read 取回、租户隔离。

见 docs/features/tool-result-offload.md。
"""

import json

import pytest

from app.core.config import settings
from app.models.entities import ToolInvocation
from app.services.tool_offload import (
    ARTIFACT_READ_TOOL_NAME,
    build_envelope,
    serialize_result,
    should_offload,
)

BIG_TEXT = "预算超支需要先冻结非必要支出。" * 1000  # 约 15KB


def _allow_echo(client, agent):
    client.post(
        "/api/v1/tools",
        json={
            "name": "echo",
            "description": "回显传入的文本",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    )
    client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": "echo", "effect": "allow"},
    )


def _last_tool_message(messages):
    """消息列表里最后一条 tool 消息。"""
    for message in reversed(messages):
        if message.get("role") == "tool":
            return message
    raise AssertionError("请求里没有 tool 消息")


def _run_with_big_result(client, agent, monkeypatch):
    """跑一次会产出大结果的运行，返回 (provider 请求记录, invocation_id)。"""
    from app.services import agent_runtime as agent_runtime_module
    from app.services.model_gateway import ModelProvider, ModelReply, StreamDone, ToolCall

    _allow_echo(client, agent)
    conversation = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={}
    ).json()

    seen: list = []

    class Scripted(ModelProvider):
        def __init__(self):
            self.replies = [
                ModelReply(
                    text="",
                    tool_calls=(
                        ToolCall(id="c1", name="echo", arguments={"text": BIG_TEXT}),
                    ),
                ),
                ModelReply(text="好的。"),
            ]

        async def generate(self, *, model, messages, tools=None):
            seen.append({"messages": list(messages), "tools": list(tools or [])})
            return self.replies.pop(0)

        async def stream(self, *, model, messages, tools=None):
            yield StreamDone(
                reply=await self.generate(model=model, messages=messages, tools=tools)
            )

    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: Scripted())

    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "查一下预算"},
    )
    assert response.status_code == 200, response.text

    envelope = json.loads(_last_tool_message(seen[1]["messages"])["content"])
    return seen, envelope["artifact"]["invocation_id"]


# ---------------------------------------------------------------------------
# 单元：剥离判定与信封
# ---------------------------------------------------------------------------


def test_should_offload_only_for_large_successful_results():
    assert should_offload(status="succeeded", serialized="x" * 4000) is True
    assert should_offload(status="succeeded", serialized="x" * 3999) is False
    # 失败与被拒不剥离：错误信息对模型判断很重要
    assert should_offload(status="failed", serialized="x" * 99999) is False
    assert should_offload(status="denied", serialized="x" * 99999) is False
    # 不可序列化的结果保持现状
    assert should_offload(status="succeeded", serialized=None) is False


def test_should_offload_respects_the_switch():
    original = settings.tool_result_offload_enabled
    settings.tool_result_offload_enabled = False
    try:
        assert should_offload(status="succeeded", serialized="x" * 99999) is False
    finally:
        settings.tool_result_offload_enabled = original


def test_envelope_is_small_and_serializable():
    serialized = serialize_result({"items": ["x" * 100 for _ in range(100)]})
    assert serialized is not None
    envelope = build_envelope(invocation_id="inv-1", serialized=serialized)

    # 信封本身必须能 JSON 序列化（它要进模型消息）
    payload = json.dumps(envelope, ensure_ascii=False)
    assert len(payload) < len(serialized) / 10

    assert envelope["result_omitted"] is True
    assert envelope["artifact"]["invocation_id"] == "inv-1"
    assert envelope["artifact"]["chars"] == len(serialized)
    assert len(envelope["artifact"]["sha256"]) == 64
    assert envelope["artifact"]["preview"]
    # 提示必须写在内容里，不能只写在系统提示里
    assert "artifact.read" in envelope["hint"]


def test_tool_result_message_keeps_small_results_untouched():
    from app.services.agent_runtime import AgentRuntime
    from app.services.model_gateway import ToolCall
    from app.services.tool_gateway import ToolCallOutcome

    outcome = ToolCallOutcome(
        invocation_id="inv-1",
        tool_name="echo",
        status="succeeded",
        allowed=True,
        reason="ok",
        result={"text": "短"},
    )
    message = AgentRuntime._tool_result_message(
        ToolCall(id="c1", name="echo", arguments={}), outcome
    )
    payload = json.loads(message["content"])
    assert payload["result"] == {"text": "短"}
    assert "result_omitted" not in payload


def test_tool_result_message_replaces_large_results_with_envelope():
    from app.services.agent_runtime import AgentRuntime
    from app.services.model_gateway import ToolCall
    from app.services.tool_gateway import ToolCallOutcome

    result = {"text": "x" * 20000}
    outcome = ToolCallOutcome(
        invocation_id="inv-2",
        tool_name="echo",
        status="succeeded",
        allowed=True,
        reason="ok",
        result=result,
    )
    serialized = serialize_result(result)
    message = AgentRuntime._tool_result_message(
        ToolCall(id="c1", name="echo", arguments={}), outcome, serialized
    )
    payload = json.loads(message["content"])
    assert payload["result_omitted"] is True
    assert payload["artifact"]["invocation_id"] == "inv-2"
    # 原始 payload 一个字都不能出现在消息里
    assert "x" * 1000 not in message["content"]
    assert len(message["content"]) < 2000


# ---------------------------------------------------------------------------
# 集成：真的跑一次运行
# ---------------------------------------------------------------------------


def test_large_tool_result_is_offloaded_from_model_context(client, agent, monkeypatch):
    seen, invocation_id = _run_with_big_result(client, agent, monkeypatch)

    # 第二次请求里，工具消息必须已经被瘦身
    message = _last_tool_message(seen[1]["messages"])
    assert len(message["content"]) < 2000
    payload = json.loads(message["content"])
    assert payload["result_omitted"] is True
    assert payload["artifact"]["chars"] > 10000
    assert invocation_id

    # 模型必须能看到 artifact.read，否则它无从取回细节
    names = [spec["function"]["name"] for spec in seen[1]["tools"]]
    assert ARTIFACT_READ_TOOL_NAME in names


def test_offloaded_result_is_still_stored_and_marked(client, agent, monkeypatch):
    from app.db.session import SessionLocal

    _seen, invocation_id = _run_with_big_result(client, agent, monkeypatch)

    with SessionLocal() as db:
        invocation = db.get(ToolInvocation, invocation_id)
        assert invocation is not None
        # 数据没丢：完整结果仍在库里
        assert invocation.result["text"] == BIG_TEXT
        # 并且被标记成「已旁路」，方便追溯
        assert invocation.offloaded is True
        assert invocation.result_chars == len(serialize_result({"text": BIG_TEXT}))


def test_model_can_read_the_offloaded_result_back(client, agent, monkeypatch):
    """模型拿到信封后能用 artifact.read 把细节读回来。"""
    from app.services import agent_runtime as agent_runtime_module
    from app.services.model_gateway import ModelProvider, ModelReply, StreamDone, ToolCall

    _allow_echo(client, agent)
    conversation = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={}
    ).json()

    requests: list = []

    class ReadBack(ModelProvider):
        async def generate(self, *, model, messages, tools=None):
            requests.append({"messages": list(messages), "tools": list(tools or [])})
            if len(requests) == 1:
                return ModelReply(
                    text="",
                    tool_calls=(
                        ToolCall(id="c1", name="echo", arguments={"text": BIG_TEXT}),
                    ),
                )
            if len(requests) == 2:
                # 从信封里取出 invocation_id——模型看到的就是这些
                envelope = json.loads(requests[1]["messages"][-1]["content"])
                return ModelReply(
                    text="",
                    tool_calls=(
                        ToolCall(
                            id="c2",
                            name=ARTIFACT_READ_TOOL_NAME,
                            arguments={
                                "invocation_id": envelope["artifact"]["invocation_id"],
                                "offset": 0,
                                "limit": 60,
                            },
                        ),
                    ),
                )
            return ModelReply(text="读完了。")

        async def stream(self, *, model, messages, tools=None):
            yield StreamDone(
                reply=await self.generate(model=model, messages=messages, tools=tools)
            )

    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: ReadBack())

    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "查一下预算"},
    )
    assert response.status_code == 200, response.text

    # 第三次请求里的工具消息是 artifact.read 的结果
    slice_payload = json.loads(_last_tool_message(requests[2]["messages"])["content"])
    assert slice_payload["status"] == "succeeded"
    result = slice_payload["result"]
    assert result["total_chars"] > 10000
    assert BIG_TEXT[:20] in result["text"]
    assert result["eof"] is False


def test_artifact_read_is_tenant_scoped(client, agent, monkeypatch, tenant_key):
    """别的租户拿不到本租户的 invocation_id。"""
    _seen, invocation_id = _run_with_big_result(client, agent, monkeypatch)

    other = tenant_key("tenant-offload-b")
    headers = {"Authorization": f"Bearer {other}"}
    other_agent = client.post(
        "/api/v1/agents", json={"name": "别的租户"}, headers=headers
    ).json()

    response = client.post(
        f"/api/v1/agents/{other_agent['id']}/tools/{ARTIFACT_READ_TOOL_NAME}/invoke",
        json={"arguments": {"invocation_id": invocation_id}},
        headers=headers,
    )
    body = response.json()
    assert body["status"] != "succeeded"
    assert body["error_code"] == "tool_not_found"


def test_artifact_read_audit_has_no_result_content(client, agent, monkeypatch):
    """审计记了 artifact.read，但不记 invocation_id 的值，也不记结果内容。"""
    _seen, invocation_id = _run_with_big_result(client, agent, monkeypatch)

    # 触发一次 artifact.read 调用（会留下审计）
    response = client.post(
        f"/api/v1/agents/{agent['id']}/tools/{ARTIFACT_READ_TOOL_NAME}/invoke",
        json={"arguments": {"invocation_id": invocation_id, "offset": 0, "limit": 10}},
    )
    assert response.json()["status"] == "succeeded", response.text

    logs = client.get("/api/v1/audit-logs", params={"limit": 200}).json()["items"]
    entries = [
        entry
        for entry in logs
        if entry["resource"] == f"tool:{ARTIFACT_READ_TOOL_NAME}"
    ]
    assert entries, "artifact.read 的调用应当留下审计"
    detail = json.dumps(entries[0]["detail"], ensure_ascii=False)
    # 只记参数键名
    assert "argument_keys" in detail
    # 不记 invocation_id 的值，也不记结果内容
    assert invocation_id not in detail
    assert BIG_TEXT[:50] not in detail


def test_offload_does_not_change_small_results(client, agent, monkeypatch):
    """小结果照旧原样回灌——既有行为不能被旁路改掉。"""
    from app.services import agent_runtime as agent_runtime_module
    from app.services.model_gateway import ModelProvider, ModelReply, StreamDone, ToolCall

    _allow_echo(client, agent)
    conversation = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={}
    ).json()

    requests: list = []

    class Small(ModelProvider):
        async def generate(self, *, model, messages, tools=None):
            requests.append({"messages": list(messages), "tools": list(tools or [])})
            if len(requests) == 1:
                return ModelReply(
                    text="",
                    tool_calls=(
                        ToolCall(id="c1", name="echo", arguments={"text": "很短"}),
                    ),
                )
            return ModelReply(text="好的。")

        async def stream(self, *, model, messages, tools=None):
            yield StreamDone(
                reply=await self.generate(model=model, messages=messages, tools=tools)
            )

    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: Small())
    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "嗨"}
    )
    assert response.status_code == 200, response.text

    payload = json.loads(_last_tool_message(requests[1]["messages"])["content"])
    assert payload["result"] == {"text": "很短"}
    assert "result_omitted" not in payload
    # 没有旁路就不该把 artifact.read 塞给模型
    names = [spec["function"]["name"] for spec in requests[1]["tools"]]
    assert ARTIFACT_READ_TOOL_NAME not in names


@pytest.mark.parametrize("limit", [0, -5])
def test_artifact_read_rejects_bad_offsets(client, agent, limit):
    """offset / limit 的边界由 JSON Schema 挡住。"""
    response = client.post(
        f"/api/v1/agents/{agent['id']}/tools/{ARTIFACT_READ_TOOL_NAME}/invoke",
        json={"arguments": {"invocation_id": "x", "offset": limit}},
    )
    assert response.status_code in (200, 403)
    body = response.json()
    assert body["status"] != "succeeded"

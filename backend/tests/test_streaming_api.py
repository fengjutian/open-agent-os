"""SSE 流式输出。"""

import json

from app.core.config import settings
from app.services import agent_runtime as agent_runtime_module
from app.services.model_gateway import ModelProvider, ModelReply, StreamDone, TextDelta, ToolCall


def parse_sse(lines) -> list[dict]:
    """把 SSE 行解析成 [{type, ...payload}]。"""
    events: list[dict] = []
    name = "message"
    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            payload = json.loads(line[5:].strip())
            events.append({"type": name, **payload})
    return events


def stream_events(client, conversation_id, content="你好"):
    with client.stream(
        "POST",
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": content},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return parse_sse(response.iter_lines())


def test_stream_emits_deltas_then_completed(client, conversation):
    events = stream_events(client, conversation["id"])

    assert events[0]["type"] == "run.started"
    assert events[0]["run_id"].startswith("run_")
    assert events[-1]["type"] == "completed"

    deltas = [e for e in events if e["type"] == "delta"]
    assert len(deltas) >= 2  # Mock 是分片输出的
    assert "Mock Agent" in "".join(e["text"] for e in deltas)


def test_stream_completed_carries_persisted_messages(client, conversation):
    events = stream_events(client, conversation["id"])
    completed = events[-1]

    assert completed["user_message"]["content"] == "你好"
    assert completed["assistant_message"]["role"] == "assistant"

    # 落库结果与流里给的一致
    history = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
    assert history["total"] == 2
    assert history["items"][1]["id"] == completed["assistant_message"]["id"]


def test_stream_run_is_persisted(client, conversation):
    events = stream_events(client, conversation["id"])
    run_id = events[0]["run_id"]

    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == "succeeded"
    assert run["iterations"] == 1
    assert run["answer_chars"] > 0

    types = [e["type"] for e in client.get(f"/api/v1/runs/{run_id}/events").json()["items"]]
    assert types[0] == "run.started"
    # run.completed 是运行生命周期记录（计数），completed 是带最终消息的交付事件，
    # 后者最后落库，供断线重连时重建结果。
    assert "run.completed" in types
    assert types[-1] == "completed"
    assert "model.request" in types


def test_stream_requires_authentication(client, conversation):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        response = anon.post(
            f"/api/v1/conversations/{conversation['id']}/messages/stream",
            json={"content": "hi"},
        )
    assert response.status_code == 401


def test_stream_enforces_quota(client, conversation, monkeypatch):
    monkeypatch.setattr(settings, "daily_run_quota", 1)

    stream_events(client, conversation["id"])

    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages/stream",
        json={"content": "again"},
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "quota_exceeded"


def test_stream_reports_model_error_as_event(client, conversation, monkeypatch):
    """建流之后出错，只能用 error 事件告知；同时运行要落库为 failed。"""
    monkeypatch.setattr(settings, "model_provider", "openai_compatible")
    monkeypatch.setattr(settings, "model_api_key", "")

    events = stream_events(client, conversation["id"])

    error = events[-1]
    assert error["type"] == "error"
    assert error["code"] == "model_not_configured"

    runs = client.get("/api/v1/runs", params={"status": "failed"}).json()
    assert runs["total"] == 1
    assert runs["items"][0]["error_code"] == "model_not_configured"


class StreamingToolProvider(ModelProvider):
    """先流式吐一点文本并请求工具，再给出最终回答。"""

    def __init__(self) -> None:
        self._step = 0

    async def stream(self, *, model, messages, tools=None):
        self._step += 1
        if self._step == 1:
            yield TextDelta(text="我先查一下")
            yield StreamDone(
                reply=ModelReply(
                    text="我先查一下",
                    tool_calls=(ToolCall(id="c1", name="echo", arguments={"text": "hi"}),),
                )
            )
            return
        yield TextDelta(text="完成")
        yield StreamDone(reply=ModelReply(text="完成"))


def test_stream_emits_tool_events(client, conversation, allow_echo, monkeypatch):
    monkeypatch.setattr(agent_runtime_module, "get_model_provider", StreamingToolProvider)

    events = stream_events(client, conversation["id"])
    types = [e["type"] for e in events]

    assert "tool.call" in types
    assert "tool.result" in types
    assert types[-1] == "completed"

    call = next(e for e in events if e["type"] == "tool.call")
    assert call["tool"] == "echo"
    result = next(e for e in events if e["type"] == "tool.result")
    assert result["status"] == "succeeded"
    assert result["result"] == {"text": "hi"}

    # 文本增量先于工具事件出现
    assert types.index("delta") < types.index("tool.call")


def test_stream_delta_text_matches_final_message(client, conversation):
    events = stream_events(client, conversation["id"])
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert streamed == events[-1]["assistant_message"]["content"]

"""SSE 断线续传：断开后运行继续，重连按 seq 回放。"""

import json
import time

import pytest


def parse_sse(lines) -> list[dict]:
    """解析 SSE：返回 [{id, type, ...payload}]。

    每条事件结束后重置 id 与事件名——`stream.end` 没有 id，
    不重置会继承上一条的 id。
    """
    events: list[dict] = []
    event_id: str | None = None
    name = "message"
    for raw in lines:
        line = raw.rstrip("\n")
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            payload = json.loads(line[5:].strip())
            events.append({"id": event_id, "type": name, **payload})
            event_id = None
            name = "message"
    return events


def start_stream(client, conversation_id, content="你好"):
    """只读第一条事件就断开，返回 (run_id, 首事件)。"""
    with client.stream(
        "POST",
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": content},
    ) as response:
        assert response.status_code == 200
        name = "message"
        for raw in response.iter_lines():
            if raw.startswith("event:"):
                name = raw[6:].strip()
            elif raw.startswith("data:"):
                payload = json.loads(raw[5:].strip())
                return payload.get("run_id"), {"type": name, **payload}
    raise AssertionError("没有收到任何事件")


def read_replay(client, run_id: str, **kwargs) -> list[dict]:
    with client.stream(
        "GET", f"/api/v1/runs/{run_id}/events/stream", params=kwargs.get("params")
    ) as response:
        assert response.status_code == 200
        return parse_sse(response.iter_lines())


def wait_for_run(client, run_id: str, *, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("运行超时未结束")


# ---------------------------------------------------------------------------
# 断开后运行继续
# ---------------------------------------------------------------------------


def test_run_survives_client_disconnect(client, conversation):
    """回归：以前客户端断开会让运行变成 failed，重连无从续起。"""
    run_id, first_event = start_stream(client, conversation["id"])
    assert first_event["type"] == "run.started"

    run = wait_for_run(client, run_id)
    assert run["status"] == "succeeded"
    assert run["error_code"] is None


def test_messages_are_persisted_after_disconnect(client, conversation):
    run_id, _ = start_stream(client, conversation["id"])
    wait_for_run(client, run_id)

    history = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
    assert history["total"] == 2
    assert history["items"][1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# 回放
# ---------------------------------------------------------------------------


def test_replay_returns_persisted_events(client, conversation):
    run_id, _ = start_stream(client, conversation["id"])
    wait_for_run(client, run_id)

    events = read_replay(client, run_id)
    types = [event["type"] for event in events]

    assert types[0] == "run.started"
    assert "completed" in types
    assert types[-1] == "stream.end"
    assert events[-1]["status"] == "succeeded"


def test_replay_delivers_final_message(client, conversation):
    run_id, _ = start_stream(client, conversation["id"])
    wait_for_run(client, run_id)

    events = read_replay(client, run_id)
    completed = next(event for event in events if event["type"] == "completed")

    assert completed["assistant_message"]["role"] == "assistant"
    assert completed["assistant_message"]["content"]
    assert completed["user_message"]["content"] == "你好"


def test_replay_events_carry_ids(client, conversation):
    run_id, _ = start_stream(client, conversation["id"])
    wait_for_run(client, run_id)

    events = read_replay(client, run_id)
    ids = [event["id"] for event in events if event["id"] is not None]

    assert ids
    assert ids == [str(number) for number in range(1, len(ids) + 1)]


def test_replay_after_cursor_skips_earlier_events(client, conversation):
    run_id, _ = start_stream(client, conversation["id"])
    wait_for_run(client, run_id)

    all_events = read_replay(client, run_id)
    tail = read_replay(client, run_id, params={"after": 2})

    # 回放从 after 之后开始，不会重复前两条
    assert tail[0]["id"] is not None
    assert int(tail[0]["id"]) > 2
    assert len(tail) < len(all_events)


def test_last_event_id_header_is_honoured(client, conversation):
    run_id, _ = start_stream(client, conversation["id"])
    wait_for_run(client, run_id)

    with client.stream(
        "GET",
        f"/api/v1/runs/{run_id}/events/stream",
        headers={"Last-Event-ID": "2"},
    ) as response:
        events = parse_sse(response.iter_lines())

    assert int(events[0]["id"]) > 2


def test_replay_of_unknown_run_returns_404(client):
    assert client.get("/api/v1/runs/run_missing/events/stream").status_code == 404


def test_replay_requires_authentication(client, conversation):
    from fastapi.testclient import TestClient

    from app.main import app

    run_id, _ = start_stream(client, conversation["id"])
    wait_for_run(client, run_id)

    with TestClient(app) as anon:
        response = anon.get(f"/api/v1/runs/{run_id}/events/stream")
    assert response.status_code == 401


@pytest.mark.parametrize("after", [-1])
def test_invalid_cursor_is_rejected(client, conversation, after):
    run_id, _ = start_stream(client, conversation["id"])
    wait_for_run(client, run_id)

    response = client.get(f"/api/v1/runs/{run_id}/events/stream", params={"after": after})
    assert response.status_code == 422

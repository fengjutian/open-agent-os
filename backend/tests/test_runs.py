"""Run / RunEvent 持久化：run_id 从日志里的字符串变成可回溯的主体。"""

import pytest


def _send(client, conversation, text="你好"):
    return client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": text},
    )


def test_run_is_persisted_with_metrics(client, conversation):
    response = _send(client, conversation)
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["id"] == run_id
    assert run["status"] == "succeeded"
    assert run["conversation_id"] == conversation["id"]
    assert run["iterations"] == 1
    assert run["tool_calls"] == 0
    assert run["answer_chars"] > 0
    assert run["prompt_chars"] > 0
    assert run["finished_at"] is not None
    assert run["error_code"] is None


def test_run_records_the_api_key(client, conversation):
    from app.db.session import SessionLocal
    from app.models.entities import ApiKey, Run

    run_id = _send(client, conversation).json()["run_id"]

    with SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None
        assert run.key_id is not None
        # key_id 必须指向真实存在的密钥
        assert db.get(ApiKey, run.key_id) is not None


def test_run_events_are_ordered_and_meaningful(client, conversation):
    run_id = _send(client, conversation).json()["run_id"]

    page = client.get(f"/api/v1/runs/{run_id}/events").json()
    events = page["items"]

    assert page["total"] == len(events)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[0]["type"] == "run.started"
    assert events[-1]["type"] == "run.completed"

    types = [e["type"] for e in events]
    assert "model.request" in types
    assert "model.response" in types


def test_tool_calls_appear_in_run_and_events(client, agent, conversation, allow_echo, monkeypatch):
    from app.services import agent_runtime as agent_runtime_module
    from app.services.model_gateway import ModelReply, ToolCall

    class Scripted:
        def __init__(self):
            self._replies = [
                ModelReply(
                    text="",
                    tool_calls=(ToolCall(id="c1", name="echo", arguments={"text": "hi"}),),
                ),
                ModelReply(text="完成"),
            ]

        async def generate(self, *, model, messages, tools=None):
            return self._replies.pop(0)

    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: Scripted())

    run_id = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "用工具"}
    ).json()["run_id"]

    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["iterations"] == 2
    assert run["tool_calls"] == 1

    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    tool_events = [e for e in events if e["type"] == "tool.result"]
    assert len(tool_events) == 1
    assert tool_events[0]["payload"]["tool"] == "echo"
    assert tool_events[0]["payload"]["status"] == "succeeded"


def test_failed_run_is_recorded(client, conversation, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "model_provider", "openai_compatible")
    monkeypatch.setattr(settings, "model_api_key", "")

    response = _send(client, conversation)
    assert response.status_code == 424

    runs = client.get("/api/v1/runs", params={"status": "failed"}).json()
    assert runs["total"] == 1
    run = runs["items"][0]
    assert run["error_code"] == "model_not_configured"
    assert run["finished_at"] is not None

    events = client.get(f"/api/v1/runs/{run['id']}/events").json()["items"]
    assert events[-1]["type"] == "run.failed"


def test_list_runs_filters(client, agent, conversation):
    _send(client, conversation)

    assert client.get("/api/v1/runs", params={"agent_id": agent["id"]}).json()["total"] == 1
    assert client.get("/api/v1/runs", params={"conversation_id": "other"}).json()["total"] == 0
    assert client.get("/api/v1/runs").json()["total"] == 1


def test_missing_run_returns_404(client):
    assert client.get("/api/v1/runs/run_does_not_exist").status_code == 404


def test_missing_run_events_returns_404(client):
    assert client.get("/api/v1/runs/run_does_not_exist/events").status_code == 404


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
def test_runs_paging_validation(client, params):
    assert client.get("/api/v1/runs", params=params).status_code == 422


def test_me_reports_role_and_permissions(client):
    body = client.get("/api/v1/me").json()

    assert body["role"] == "admin"
    assert body["key_id"]
    assert "write:tools" in body["permissions"]

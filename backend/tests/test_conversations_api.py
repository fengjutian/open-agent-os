"""会话创建、分页与「最近活动」排序。"""

import time

from app.models.entities import now_utc

# 时间戳精度在 Windows 上约 15ms，留出余量再比较先后。
_STEP = 0.05


def test_create_conversation(client, agent):
    response = client.post(
        f"/api/v1/agents/{agent['id']}/conversations",
        json={"title": "第一个会话"},
    )
    assert response.status_code == 201

    body = response.json()
    assert body["agent_id"] == agent["id"]
    assert body["title"] == "第一个会话"


def test_create_conversation_for_missing_agent_returns_404(client):
    response = client.post("/api/v1/agents/nope/conversations", json={"title": "t"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_list_conversations_is_scoped_to_agent(client):
    first = client.post("/api/v1/agents", json={"name": "a1"}).json()
    second = client.post("/api/v1/agents", json={"name": "a2"}).json()
    client.post(f"/api/v1/agents/{first['id']}/conversations", json={"title": "only-a1"})

    page = client.get(f"/api/v1/agents/{first['id']}/conversations").json()
    assert page["total"] == 1
    assert client.get(f"/api/v1/agents/{second['id']}/conversations").json()["total"] == 0


def test_conversation_list_order_follows_latest_activity(client, agent):
    """回归：发消息曾不推进 updated_at，导致活跃会话不会排到最前。"""
    older = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={"title": "older"}
    ).json()
    time.sleep(_STEP)
    newer = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={"title": "newer"}
    ).json()

    ids = [c["id"] for c in client.get(f"/api/v1/agents/{agent['id']}/conversations").json()["items"]]
    assert ids == [newer["id"], older["id"]]

    # 给旧会话发一条消息，它应该回到列表最前
    time.sleep(_STEP)
    assert (
        client.post(
            f"/api/v1/conversations/{older['id']}/messages", json={"content": "hi"}
        ).status_code
        == 200
    )

    ids = [c["id"] for c in client.get(f"/api/v1/agents/{agent['id']}/conversations").json()["items"]]
    assert ids == [older["id"], newer["id"]]


def test_sending_message_advances_conversation_updated_at(client, conversation):
    """回归：updated_at 曾完全不随新消息变化。"""
    before = conversation["updated_at"]
    time.sleep(_STEP)

    client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"})

    after = client.get(f"/api/v1/agents/{conversation['agent_id']}/conversations").json()["items"][0]
    assert after["updated_at"] > before
    assert after["updated_at"] <= now_utc().isoformat()

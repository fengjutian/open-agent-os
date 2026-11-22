"""消息发送、历史、分页与模型错误映射。"""

import pytest

from app.core.config import settings


def test_send_message_returns_run_id_and_both_messages(client, conversation):
    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "你好"},
    )
    assert response.status_code == 200

    body = response.json()
    assert body["run_id"].startswith("run_")
    assert body["user_message"]["role"] == "user"
    assert body["user_message"]["content"] == "你好"
    assert body["assistant_message"]["role"] == "assistant"
    assert body["assistant_message"]["content"]


def test_send_message_persists_history_in_order(client, conversation):
    client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "一"})
    client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "二"})

    page = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
    assert page["total"] == 4
    assert [m["role"] for m in page["items"]] == ["user", "assistant"] * 2
    assert [m["content"] for m in page["items"]][0] == "一"


@pytest.mark.parametrize(
    "content",
    ["", "x" * 30001],
    ids=["empty", "over-max-length"],
)
def test_send_message_validates_content(client, conversation, content):
    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": content},
    )
    assert response.status_code == 422


def test_send_message_to_missing_conversation_returns_404(client):
    response = client.post("/api/v1/conversations/nope/messages", json={"content": "hi"})
    assert response.status_code == 404


def test_list_messages_pagination(client, conversation):
    for index in range(3):
        client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={"content": f"m{index}"},
        )

    page = client.get(
        f"/api/v1/conversations/{conversation['id']}/messages", params={"limit": 2}
    ).json()
    assert page["total"] == 6
    assert len(page["items"]) == 2

    tail = client.get(
        f"/api/v1/conversations/{conversation['id']}/messages",
        params={"limit": 2, "offset": 4},
    ).json()
    assert len(tail["items"]) == 2
    assert tail["items"][-1]["role"] == "assistant"


def test_model_not_configured_maps_to_424_without_leaking_secrets(
    client, conversation, monkeypatch
):
    monkeypatch.setattr(settings, "model_provider", "openai_compatible")
    monkeypatch.setattr(settings, "model_api_key", "")

    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "hi"},
    )
    assert response.status_code == 424

    error = response.json()["error"]
    assert error["code"] == "model_not_configured"
    assert "模型未配置" in error["message"]
    assert "sk-" not in response.text


def test_failed_run_rolls_back_user_message(client, conversation, monkeypatch):
    monkeypatch.setattr(settings, "model_provider", "openai_compatible")
    monkeypatch.setattr(settings, "model_api_key", "")

    client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"})

    # 模型调用失败时，用户消息不应半落库
    page = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
    assert page["total"] == 0

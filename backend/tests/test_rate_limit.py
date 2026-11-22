"""请求限流与每日运行配额。"""

from app.core.config import settings


def test_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)

    for _ in range(3):
        assert client.get("/api/v1/agents").status_code == 200

    response = client.get("/api/v1/agents")

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["details"]["limit"] == 3
    assert int(response.headers["Retry-After"]) >= 1


def test_rate_limit_counts_per_key(client, make_key, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)

    assert client.get("/api/v1/agents").status_code == 200
    assert client.get("/api/v1/agents").status_code == 200
    assert client.get("/api/v1/agents").status_code == 429

    # 换一把密钥应当重新计数，而不是被同一把密钥拖累
    client.headers["Authorization"] = f"Bearer {make_key('admin')}"
    assert client.get("/api/v1/agents").status_code == 200


def test_rate_limit_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)

    for _ in range(10):
        assert client.get("/api/v1/agents").status_code == 200


def test_health_is_not_rate_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)

    for _ in range(5):
        assert client.get("/health").status_code == 200


def test_run_quota_returns_429(client, conversation, monkeypatch):
    monkeypatch.setattr(settings, "daily_run_quota", 2)

    def send():
        return client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={"content": "hi"},
        )

    assert send().status_code == 200
    assert send().status_code == 200

    response = send()
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "quota_exceeded"
    assert error["details"]["limit"] == 2
    assert error["details"]["used"] >= 2


def test_run_quota_is_per_key(client, conversation, make_key, monkeypatch):
    monkeypatch.setattr(settings, "daily_run_quota", 1)

    def send():
        return client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={"content": "hi"},
        )

    assert send().status_code == 200
    assert send().status_code == 429

    client.headers["Authorization"] = f"Bearer {make_key('admin')}"
    assert send().status_code == 200


def test_quota_counts_failed_runs(client, conversation, monkeypatch):
    """失败的运行也占用配额——否则可以靠制造失败来绕过限额。"""
    monkeypatch.setattr(settings, "daily_run_quota", 1)
    monkeypatch.setattr(settings, "model_provider", "openai_compatible")
    monkeypatch.setattr(settings, "model_api_key", "")

    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"}
    )
    assert response.status_code == 424

    second = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"}
    )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "quota_exceeded"

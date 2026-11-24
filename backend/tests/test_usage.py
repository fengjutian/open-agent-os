"""用量报表：总量、时间桶序列、维度分解。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.session import SessionLocal
from app.models.entities import Run, ToolInvocation


def _insert_run(
    *,
    run_id: str,
    agent_id: str | None = None,
    model: str = "gpt-4o-mini",
    status: str = "succeeded",
    started_at: datetime | None = None,
    duration_seconds: float = 2.0,
    tool_calls: int = 0,
    prompt_chars: int = 100,
    answer_chars: int = 50,
    tenant_id: str = "default",
) -> None:
    start = started_at or datetime.now(UTC)
    with SessionLocal() as db:
        db.add(
            Run(
                id=run_id,
                agent_id=agent_id,
                status=status,
                model=model,
                iterations=1,
                tool_calls=tool_calls,
                prompt_chars=prompt_chars,
                answer_chars=answer_chars,
                started_at=start,
                finished_at=start + timedelta(seconds=duration_seconds),
                tenant_id=tenant_id,
            )
        )
        db.commit()


def _insert_tool(
    *,
    run_id: str,
    tool_name: str = "echo",
    status: str = "succeeded",
    started_at: datetime | None = None,
    duration_seconds: float = 0.5,
    tenant_id: str = "default",
) -> None:
    start = started_at or datetime.now(UTC)
    with SessionLocal() as db:
        db.add(
            ToolInvocation(
                run_id=run_id,
                tool_name=tool_name,
                arguments={},
                result={"ok": True},
                status=status,
                started_at=start,
                finished_at=start + timedelta(seconds=duration_seconds),
                tenant_id=tenant_id,
            )
        )
        db.commit()


# ---------------------------------------------------------------------------
# 总量
# ---------------------------------------------------------------------------


def test_overview_counts_runs_and_characters(client):
    _insert_run(run_id="run_u1", prompt_chars=120, answer_chars=30)
    _insert_run(
        run_id="run_u2",
        status="failed",
        tool_calls=2,
        prompt_chars=0,
        answer_chars=0,
    )

    body = client.get("/api/v1/usage/overview").json()
    assert body["runs"] == 2
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert body["errors"] == 1
    assert body["tool_calls"] == 2
    assert body["prompt_chars"] == 120
    assert body["answer_chars"] == 30
    assert body["avg_duration_ms"] == 2000
    assert body["days"] == 30


def test_overview_counts_active_dimensions(client, agent):
    _insert_run(run_id="run_u3", agent_id=agent["id"], model="gpt-4o")
    _insert_run(run_id="run_u4", agent_id=agent["id"], model="gpt-4o")
    _insert_run(run_id="run_u5", agent_id=agent["id"], model="gpt-4o-mini")
    _insert_tool(run_id="run_u3", tool_name="echo")

    body = client.get("/api/v1/usage/overview").json()
    assert body["agents"] == 1
    assert body["models"] == 2
    assert body["tools"] == 1


def test_overview_excludes_runs_outside_the_window(client):
    old = datetime.now(UTC) - timedelta(days=40)
    _insert_run(run_id="run_old", started_at=old)

    assert client.get("/api/v1/usage/overview", params={"days": 30}).json()["runs"] == 0
    assert client.get("/api/v1/usage/overview", params={"days": 60}).json()["runs"] == 1


# ---------------------------------------------------------------------------
# 时间序列
# ---------------------------------------------------------------------------


def test_timeline_fills_days_without_runs(client):
    today = datetime.now(UTC)
    _insert_run(run_id="run_t1", started_at=today - timedelta(days=3))

    body = client.get("/api/v1/usage/timeline", params={"days": 7, "bucket": "day"}).json()
    assert len(body) == 8
    assert sum(item["runs"] for item in body) == 1
    # 没有数据的那天也要出现，且计数为 0
    empty = [item for item in body if item["runs"] == 0]
    assert empty
    assert all(item["key"] for item in body)


def test_timeline_buckets_by_month(client):
    now = datetime.now(UTC)
    _insert_run(run_id="run_m1", started_at=now - timedelta(days=200))

    body = client.get("/api/v1/usage/timeline", params={"days": 365, "bucket": "month"}).json()
    assert len(body) >= 2
    assert sum(item["runs"] for item in body) == 1
    assert all(len(item["key"]) == 7 for item in body)  # YYYY-MM


def test_timeline_rejects_unknown_bucket(client):
    response = client.get("/api/v1/usage/timeline", params={"bucket": "hour"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 维度分解
# ---------------------------------------------------------------------------


def test_breakdown_by_agent_uses_names(client, agent):
    _insert_run(run_id="run_a1", agent_id=agent["id"])
    _insert_run(run_id="run_a2", agent_id=agent["id"])

    body = client.get("/api/v1/usage/breakdown", params={"dimension": "agent"}).json()
    assert len(body) == 1
    assert body[0]["key"] == agent["id"]
    assert body[0]["label"] == agent["name"]
    assert body[0]["runs"] == 2


def test_breakdown_by_model(client):
    _insert_run(run_id="run_b1", model="gpt-4o")
    _insert_run(run_id="run_b2", model="gpt-4o-mini")

    body = client.get("/api/v1/usage/breakdown", params={"dimension": "model"}).json()
    keys = {item["key"]: item["runs"] for item in body}
    assert keys == {"gpt-4o": 1, "gpt-4o-mini": 1}


def test_breakdown_by_tool_counts_invocations(client):
    _insert_run(run_id="run_c1", tool_calls=2)
    _insert_tool(run_id="run_c1", tool_name="echo")
    _insert_tool(run_id="run_c1", tool_name="echo")
    _insert_tool(run_id="run_c1", tool_name="fetch", status="failed")

    body = client.get("/api/v1/usage/breakdown", params={"dimension": "tool"}).json()
    stats = {item["key"]: item for item in body}
    assert stats["echo"]["tool_calls"] == 2
    assert stats["echo"]["runs"] == 1
    assert stats["echo"]["errors"] == 0
    assert stats["fetch"]["tool_calls"] == 1
    assert stats["fetch"]["errors"] == 1


def test_breakdown_rejects_unknown_dimension(client):
    response = client.get("/api/v1/usage/breakdown", params={"dimension": "nope"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 边界与隔离
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("params", [{"days": 0}, {"days": 1000}])
def test_days_bounds_are_enforced(client, params):
    assert client.get("/api/v1/usage/overview", params=params).status_code == 422


def test_usage_is_tenant_scoped(client, tenant_key):
    _insert_run(run_id="run_t2", tenant_id="default")

    other = tenant_key("tenant-usage-b")
    headers = {"Authorization": f"Bearer {other}"}
    assert client.get("/api/v1/usage/overview", headers=headers).json()["runs"] == 0
    assert (
        client.get(
            "/api/v1/usage/breakdown", params={"dimension": "agent"}, headers=headers
        ).json()
        == []
    )


def test_usage_is_readable_by_viewer(client, make_key):
    """用量报表是只读数据，viewer 角色也能看。"""
    viewer = make_key("viewer")
    response = client.get(
        "/api/v1/usage/overview", headers={"Authorization": f"Bearer {viewer}"}
    )
    assert response.status_code == 200

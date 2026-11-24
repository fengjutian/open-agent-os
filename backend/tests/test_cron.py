"""定时任务：表达式解析、CRUD、调度执行。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.db.session import SessionLocal
from app.models.entities import CronJob, now_utc
from app.services.cron import CronError, next_run, parse_schedule
from app.services.cron_scheduler import run_due_jobs

H = lambda raw: {"Authorization": f"Bearer {raw}"}  # noqa: E731


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# 表达式
# ---------------------------------------------------------------------------


def test_next_run_every_five_minutes():
    assert next_run(parse_schedule("*/5 * * * *"), at("2026-01-01 10:02")) == at(
        "2026-01-01 10:05"
    )


def test_next_run_daily_rolls_to_tomorrow():
    assert next_run(parse_schedule("0 9 * * *"), at("2026-01-01 10:00")) == at(
        "2026-01-02 09:00"
    )


def test_next_run_monthly_rolls_to_next_month():
    assert next_run(parse_schedule("0 0 1 * *"), at("2026-01-15 12:00")) == at(
        "2026-02-01 00:00"
    )


def test_next_run_weekday_semantics():
    # 2026-01-01 是周四；cron 里 1 = 周一
    assert next_run(parse_schedule("30 8 * * 1"), at("2026-01-01 09:00")) == at(
        "2026-01-05 08:30"
    )


def test_next_run_day_of_month_or_weekday():
    # 13 号或周五，取先到的那个：2026-01-01 之后最近的周五是 1/2
    result = next_run(parse_schedule("0 0 13 * 5"), at("2026-01-01 00:30"))
    assert result == at("2026-01-02 00:00")


def test_alias_daily():
    assert next_run(parse_schedule("@daily"), at("2026-01-01 10:00")) == at(
        "2026-01-02 00:00"
    )


@pytest.mark.parametrize(
    "expression",
    ["*/0 * * * *", "60 * * * *", "1 2 3", "abc def ghi jkl mno", "", "0 0 32 * *"],
)
def test_invalid_expressions(expression):
    with pytest.raises(CronError):
        parse_schedule(expression)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def _create(client, agent_id: str, **overrides) -> dict:
    payload = {
        "name": "每日汇报",
        "prompt": "总结今天的进展",
        "schedule": "0 9 * * *",
        **overrides,
    }
    response = client.post(f"/api/v1/agents/{agent_id}/cron", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_cron_job(client, agent):
    job = _create(client, agent["id"])

    assert job["schedule"] == "0 9 * * *"
    assert job["enabled"] is True
    assert job["last_status"] is None
    # 下次执行时间必须在未来
    assert datetime.fromisoformat(job["next_run_at"].replace("Z", "+00:00")) > now_utc()


def test_create_cron_job_with_invalid_schedule(client, agent):
    response = client.post(
        f"/api/v1/agents/{agent['id']}/cron",
        json={"name": "bad", "prompt": "x", "schedule": "not a cron"},
    )
    assert response.status_code == 422
    assert "cron 表达式非法" in response.json()["error"]["message"]


def test_list_cron_jobs(client, agent):
    _create(client, agent["id"], name="a")
    _create(client, agent["id"], name="b")

    page = client.get(f"/api/v1/agents/{agent['id']}/cron").json()
    assert page["total"] == 2
    assert {item["name"] for item in page["items"]} == {"a", "b"}


def test_patch_cron_job_recomputes_next_run(client, agent):
    job = _create(client, agent["id"])

    response = client.patch(f"/api/v1/cron/{job['id']}", json={"schedule": "*/5 * * * *"})
    assert response.status_code == 200
    updated = response.json()
    assert updated["schedule"] == "*/5 * * * *"
    assert updated["next_run_at"] != job["next_run_at"]


def test_patch_cron_job_rejects_bad_schedule(client, agent):
    job = _create(client, agent["id"])
    response = client.patch(f"/api/v1/cron/{job['id']}", json={"schedule": "99 99 * * *"})
    assert response.status_code == 422


def test_delete_cron_job(client, agent):
    job = _create(client, agent["id"])
    assert client.delete(f"/api/v1/cron/{job['id']}").status_code == 204
    assert client.get(f"/api/v1/cron/{job['id']}").status_code == 404


def test_cron_creation_requires_write_permission(client, agent, make_key):
    viewer = make_key("viewer")
    response = client.post(
        f"/api/v1/agents/{agent['id']}/cron",
        json={"name": "x", "prompt": "x", "schedule": "0 9 * * *"},
        headers=H(viewer),
    )
    assert response.status_code == 403


def test_cron_job_is_audited(client, agent):
    _create(client, agent["id"])
    logs = client.get("/api/v1/audit-logs", params={"action": "cron.create"}).json()
    assert logs["total"] == 1


def test_cron_jobs_are_isolated_between_tenants(client, agent, tenant_key):
    _create(client, agent["id"])
    other = tenant_key("tenant-b")

    assert client.get(f"/api/v1/agents/{agent['id']}/cron", headers=H(other)).status_code == 404


def test_unknown_agent_returns_404(client):
    response = client.post(
        "/api/v1/agents/missing/cron",
        json={"name": "x", "prompt": "x", "schedule": "0 9 * * *"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def test_run_now_executes_once(client, agent, conversation):
    job = _create(client, agent["id"], conversation_id=conversation["id"])

    response = client.post(f"/api/v1/cron/{job['id']}/run-now")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["last_status"] == "succeeded"
    assert body["last_run_at"] is not None

    messages = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
    assert messages["total"] == 2
    assert messages["items"][1]["role"] == "assistant"


def test_run_now_creates_conversation_when_missing(client, agent):
    job = _create(client, agent["id"])
    assert job["conversation_id"] is None

    body = client.post(f"/api/v1/cron/{job['id']}/run-now").json()
    assert body["conversation_id"]
    # 后续执行复用同一个会话
    assert client.post(f"/api/v1/cron/{job['id']}/run-now").json()["conversation_id"] == body[
        "conversation_id"
    ]


def _make_due(job_id: str, when: datetime | None = None) -> None:
    with SessionLocal() as db:
        db.execute(
            update(CronJob)
            .where(CronJob.id == job_id)
            .values(next_run_at=when or (now_utc() - timedelta(minutes=1)))
        )
        db.commit()


def test_due_jobs_run_and_advance(client, agent, conversation):
    job = _create(client, agent["id"], conversation_id=conversation["id"])
    _make_due(job["id"])

    executed = asyncio.run(run_due_jobs(now=now_utc()))
    assert executed == [job["id"]]

    with SessionLocal() as db:
        row = db.get(CronJob, job["id"])
        assert row.last_status == "succeeded"
        # SQLite 读回来是 naive，比较前补成 UTC
        assert _aware(row.next_run_at) > now_utc()

    # 同一个时间点再跑一次不应该重复执行
    assert asyncio.run(run_due_jobs(now=now_utc())) == []


def test_disabled_job_is_skipped(client, agent):
    job = _create(client, agent["id"], enabled=False)
    _make_due(job["id"])

    assert asyncio.run(run_due_jobs(now=now_utc())) == []


def test_failed_job_records_error(client, agent):
    job = _create(client, agent["id"])
    # 直接删掉 Agent，模拟"任务还在但 Agent 没了"
    with SessionLocal() as db:
        from app.models.entities import Agent

        db.delete(db.get(Agent, agent["id"]))
        db.commit()
    _make_due(job["id"])

    asyncio.run(run_due_jobs(now=now_utc()))

    with SessionLocal() as db:
        row = db.get(CronJob, job["id"])
        assert row.last_status == "failed"
        assert "Agent 不存在" in (row.last_error or "")


def test_due_jobs_are_tenant_scoped(client, agent, conversation, tenant_key):
    other = tenant_key("tenant-b")
    other_agent = client.post(
        "/api/v1/agents", json={"name": "b-agent", "model": "gpt-4o-mini"}, headers=H(other)
    ).json()
    other_conv = client.post(
        f"/api/v1/agents/{other_agent['id']}/conversations", json={}, headers=H(other)
    ).json()

    job = client.post(
        f"/api/v1/agents/{other_agent['id']}/cron",
        json={
            "name": "tenant-b job",
            "prompt": "在 B 租户里跑",
            "schedule": "0 9 * * *",
            "conversation_id": other_conv["id"],
        },
        headers=H(other),
    ).json()
    _make_due(job["id"])

    asyncio.run(run_due_jobs(now=now_utc()))

    # B 租户自己的会话里出现了消息
    messages = client.get(
        f"/api/v1/conversations/{other_conv['id']}/messages", headers=H(other)
    ).json()
    assert messages["total"] == 2

    # 默认租户看不到这个任务，也看不到它的消息
    assert client.get(f"/api/v1/cron/{job['id']}").status_code == 404
    assert client.get(f"/api/v1/conversations/{other_conv['id']}/messages").status_code == 404

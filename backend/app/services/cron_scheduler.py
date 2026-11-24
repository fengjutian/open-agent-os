"""定时任务的执行与调度循环。

调度是**进程内轮询**：单实例够用；多实例部署会重复执行，需要外部分布式锁。
这一点在文档里明说，不假装支持。

执行一条任务时：

1. 先切到任务所属租户（`set_tenant`），否则租户过滤会让它查不到自己的 Agent；
2. 用独立会话跑一次非流式运行；
3. 无论成败都用**另一个会话**回写状态——运行失败时会回滚，状态不能跟着丢。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from app.core.config import settings
from app.core.tenancy import reset_tenant, set_tenant
from app.db.session import SessionLocal
from app.models.entities import Agent, Conversation, CronJob, now_utc
from app.services.agent_runtime import AgentRuntime
from app.services.cron import CronError, next_run, parse_schedule

logger = logging.getLogger(__name__)


async def run_job(job_id: str, *, tenant_id: str) -> dict:
    """执行一条任务，返回 {status, error, conversation_id}。"""
    token = set_tenant(tenant_id)
    status = "failed"
    error: str | None = None
    conversation_id: str | None = None

    try:
        with SessionLocal() as db:
            job = db.get(CronJob, job_id)
            if job is None:
                return {"status": "missing", "error": "任务不存在", "conversation_id": None}

            agent = db.get(Agent, job.agent_id)
            if agent is None:
                raise RuntimeError("Agent 不存在")

            conversation = (
                db.get(Conversation, job.conversation_id) if job.conversation_id else None
            )
            if conversation is None:
                conversation = Conversation(agent_id=agent.id, title=f"定时任务：{job.name}")
                db.add(conversation)
                db.flush()

            conversation_id = conversation.id
            await AgentRuntime().respond(db, conversation, job.prompt)
            db.commit()
            status = "succeeded"
    except Exception as exc:  # noqa: BLE001 - 任务失败不该拖垮调度循环
        logger.exception("cron job failed", extra={"fields": {"job_id": job_id}})
        error = str(exc)[:500]
    finally:
        if status != "succeeded":
            with SessionLocal() as db:
                job = db.get(CronJob, job_id)
                if job is not None:
                    job.last_run_at = now_utc()
                    job.last_status = status
                    job.last_error = error
                    db.commit()
        else:
            with SessionLocal() as db:
                job = db.get(CronJob, job_id)
                if job is not None:
                    job.last_run_at = now_utc()
                    job.last_status = status
                    job.last_error = None
                    if job.conversation_id is None and conversation_id:
                        job.conversation_id = conversation_id
                    db.commit()
        reset_tenant(token)

    return {"status": status, "error": error, "conversation_id": conversation_id}


async def run_due_jobs(*, now: datetime | None = None, limit: int | None = None) -> list[str]:
    """执行所有到期任务，返回本次触发的 job id 列表。

    先推进 `next_run_at` 并提交，再逐个执行——即使执行阶段崩溃，
    也不会在下一个 tick 重复触发同一条任务。
    """
    moment = now or now_utc()
    due: list[tuple[str, str]] = []

    with SessionLocal() as db:
        rows = db.scalars(
            select(CronJob)
            .where(CronJob.enabled.is_(True), CronJob.next_run_at <= moment)
            .order_by(CronJob.next_run_at)
            .limit(limit or settings.cron_max_due_per_tick)
            .execution_options(include_all_tenants=True)
        ).all()

        for job in rows:
            due.append((job.id, job.tenant_id))
            try:
                job.next_run_at = next_run(parse_schedule(job.schedule), moment)
            except CronError as exc:
                job.enabled = False
                job.last_status = "failed"
                job.last_error = f"表达式已失效：{exc}"

        db.commit()

    for job_id, tenant_id in due:
        await run_job(job_id, tenant_id=tenant_id)

    return [job_id for job_id, _ in due]


async def scheduler_loop() -> None:
    """后台轮询。由 lifespan 启动，退出时取消。"""
    logger.info(
        "cron scheduler started",
        extra={"fields": {"tick_seconds": settings.cron_tick_seconds}},
    )
    while True:
        try:
            await run_due_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 一轮失败不影响后续
            logger.exception("cron tick failed")
        await asyncio.sleep(max(1.0, settings.cron_tick_seconds))

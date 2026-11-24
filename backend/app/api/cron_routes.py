"""定时任务：按 cron 表达式周期性让 Agent 跑一次提示词。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, principal_actor, require_permission
from app.api.helpers import get_or_404
from app.core.errors import AppError, ErrorCode
from app.core.security import READ, WRITE_AGENTS, Principal
from app.db.session import get_db
from app.models.entities import Agent, Conversation, CronJob, now_utc
from app.schemas.dto import CronJobCreate, CronJobRead, CronJobUpdate, ErrorEnvelope, Page
from app.services.audit import AuditService
from app.services.cron import CronError, next_run, parse_schedule
from app.services.cron_scheduler import run_job

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "未认证"},
    403: {"model": ErrorEnvelope, "description": "无权限"},
    404: {"model": ErrorEnvelope, "description": "资源不存在"},
    422: {"model": ErrorEnvelope, "description": "cron 表达式或参数非法"},
}

router = APIRouter(prefix="/api/v1", tags=["cron"], dependencies=[Depends(get_principal)])


def _next_run_at(schedule: str):
    """把非法表达式转成 422，而不是 500。"""
    try:
        return next_run(parse_schedule(schedule), now_utc())
    except CronError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"cron 表达式非法：{exc}",
            status_code=422,
        ) from exc


@router.post(
    "/agents/{agent_id}/cron",
    response_model=CronJobRead,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def create_cron_job(
    agent_id: str,
    payload: CronJobCreate,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    get_or_404(db, Agent, agent_id, "Agent")
    if payload.conversation_id is not None:
        get_or_404(db, Conversation, payload.conversation_id, "Conversation")

    job = CronJob(
        agent_id=agent_id,
        conversation_id=payload.conversation_id,
        name=payload.name,
        prompt=payload.prompt,
        schedule=payload.schedule.strip(),
        enabled=payload.enabled,
        next_run_at=_next_run_at(payload.schedule),
    )
    db.add(job)
    db.flush()
    AuditService(db).record(
        action="cron.create",
        actor=principal_actor(principal),
        resource=f"cron:{job.id}",
        decision="allow",
        reason="创建定时任务",
        detail={"agent_id": agent_id, "schedule": job.schedule, "enabled": job.enabled},
    )
    db.commit()
    db.refresh(job)
    return job


@router.get(
    "/agents/{agent_id}/cron",
    response_model=Page[CronJobRead],
    responses=_ERRORS,
)
def list_cron_jobs(
    agent_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    get_or_404(db, Agent, agent_id, "Agent")
    total = (
        db.scalar(select(func.count()).select_from(CronJob).where(CronJob.agent_id == agent_id))
        or 0
    )
    items = db.scalars(
        select(CronJob)
        .where(CronJob.agent_id == agent_id)
        .order_by(CronJob.created_at.desc(), CronJob.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[CronJobRead](items=items, total=total, limit=limit, offset=offset)


@router.get("/cron/{job_id}", response_model=CronJobRead, responses=_ERRORS)
def get_cron_job(
    job_id: str,
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    return get_or_404(db, CronJob, job_id, "Cron job")


@router.patch("/cron/{job_id}", response_model=CronJobRead, responses=_ERRORS)
def update_cron_job(
    job_id: str,
    payload: CronJobUpdate,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    job = get_or_404(db, CronJob, job_id, "Cron job")

    if payload.name is not None:
        job.name = payload.name
    if payload.prompt is not None:
        job.prompt = payload.prompt
    if payload.schedule is not None:
        job.schedule = payload.schedule.strip()
        job.next_run_at = _next_run_at(payload.schedule)
    if payload.conversation_id is not None:
        get_or_404(db, Conversation, payload.conversation_id, "Conversation")
        job.conversation_id = payload.conversation_id
    if payload.enabled is not None:
        job.enabled = payload.enabled
        # 重新启用时把下次执行时间挪到未来，避免"一开就补跑一堆"
        if payload.enabled and job.next_run_at <= now_utc():
            job.next_run_at = _next_run_at(job.schedule)

    AuditService(db).record(
        action="cron.update",
        actor=principal_actor(principal),
        resource=f"cron:{job.id}",
        decision="allow",
        reason="更新定时任务",
        detail={"schedule": job.schedule, "enabled": job.enabled},
    )
    db.commit()
    db.refresh(job)
    return job


@router.delete("/cron/{job_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
def delete_cron_job(
    job_id: str,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    job = get_or_404(db, CronJob, job_id, "Cron job")
    AuditService(db).record(
        action="cron.delete",
        actor=principal_actor(principal),
        resource=f"cron:{job.id}",
        decision="allow",
        reason="删除定时任务",
        detail={"agent_id": job.agent_id, "schedule": job.schedule},
    )
    db.delete(job)
    db.commit()
    return None


@router.post("/cron/{job_id}/run-now", response_model=CronJobRead, responses=_ERRORS)
async def run_cron_job_now(
    job_id: str,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    """立即执行一次，不改变原有排期。"""
    job = get_or_404(db, CronJob, job_id, "Cron job")
    tenant_id = job.tenant_id
    AuditService(db).record(
        action="cron.run",
        actor=principal_actor(principal),
        resource=f"cron:{job.id}",
        decision="allow",
        reason="手动触发定时任务",
        detail={"agent_id": job.agent_id},
    )
    db.commit()

    await run_job(job_id, tenant_id=tenant_id)

    db.expire_all()
    return get_or_404(db, CronJob, job_id, "Cron job")

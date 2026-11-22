"""运行记录查询与当前身份。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, require_permission
from app.api.helpers import count_rows, get_or_404
from app.core.security import READ, Principal
from app.db.session import get_db
from app.models.entities import Run, RunEvent
from app.schemas.dto import ErrorEnvelope, MeRead, Page, RunEventRead, RunRead

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "未认证"},
    403: {"model": ErrorEnvelope, "description": "无权限"},
    404: {"model": ErrorEnvelope, "description": "资源不存在"},
}

router = APIRouter(prefix="/api/v1", tags=["runs"], dependencies=[Depends(get_principal)])


@router.get("/me", response_model=MeRead)
def whoami(principal: Principal = Depends(get_principal)):
    return MeRead(
        key_id=principal.key_id,
        name=principal.name,
        role=principal.role,
        permissions=sorted(principal.permissions),
    )


@router.get("/runs", response_model=Page[RunRead])
def list_runs(
    agent_id: str | None = Query(default=None),
    conversation_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    conditions = []
    if agent_id:
        conditions.append(Run.agent_id == agent_id)
    if conversation_id:
        conditions.append(Run.conversation_id == conversation_id)
    if status:
        conditions.append(Run.status == status)

    total = count_rows(db, Run, *conditions)
    statement = select(Run)
    if conditions:
        statement = statement.where(*conditions)
    items = db.scalars(
        statement.order_by(Run.started_at.desc(), Run.id).limit(limit).offset(offset)
    ).all()
    return Page[RunRead](items=items, total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=RunRead, responses=_ERRORS)
def get_run(
    run_id: str,
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    return get_or_404(db, Run, run_id, "Run")


@router.get("/runs/{run_id}/events", response_model=Page[RunEventRead], responses=_ERRORS)
def list_run_events(
    run_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    get_or_404(db, Run, run_id, "Run")
    total = count_rows(db, RunEvent, RunEvent.run_id == run_id)
    items = db.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id)
        .order_by(RunEvent.seq)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[RunEventRead](items=items, total=total, limit=limit, offset=offset)

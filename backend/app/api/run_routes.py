"""运行记录查询与当前身份。"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, require_permission
from app.api.helpers import count_rows, get_or_404
from app.api.sse import sse_event
from app.core.config import settings
from app.core.security import READ, Principal
from app.db.session import SessionLocal, get_db
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
        auth_method=principal.auth_method,
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


@router.get(
    "/runs/{run_id}/events/stream",
    responses={
        200: {
            "description": "SSE 事件回放：按 seq 续传，运行未结束时持续跟随",
            "content": {"text/event-stream": {}},
        },
        **_ERRORS,
    },
)
async def stream_run_events(
    run_id: str,
    after: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    """断线续传：回放某次运行已落库的事件。

    游标来自 `after` 查询参数或标准的 `Last-Event-ID` 头（两者取其一，`after` 优先）。
    运行仍在进行时会持续跟随，直到进入终态或超过 `RUN_STREAM_MAX_SECONDS`。

    注意：**增量文本不落库**，重连能拿到生命周期事件与最终消息，
    但拿不回断流前已经流出的那段文字。
    """
    get_or_404(db, Run, run_id, "Run")

    cursor = after
    if cursor is None:
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

    poll = settings.run_stream_poll_seconds
    deadline = time.monotonic() + settings.run_stream_max_seconds

    async def event_stream():
        # 回放期间运行任务在写，独立会话 + WAL 保证两者不互相阻塞
        session = SessionLocal()
        emitted = cursor
        try:
            while True:
                rows = session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.seq > emitted)
                    .order_by(RunEvent.seq)
                ).all()
                for row in rows:
                    emitted = row.seq
                    yield sse_event({**row.payload, "type": row.type}, event_id=row.seq)

                # 结束只读事务，避免长时间持有快照
                session.commit()

                run = session.get(Run, run_id)
                status = run.status if run is not None else "missing"
                if status != "running":
                    yield sse_event({"type": "stream.end", "run_id": run_id, "status": status})
                    return
                if time.monotonic() > deadline:
                    yield sse_event(
                        {
                            "type": "stream.end",
                            "run_id": run_id,
                            "status": status,
                            "reason": "timeout",
                        }
                    )
                    return
                await asyncio.sleep(poll)
        finally:
            session.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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

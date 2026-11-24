"""用量报表：总量、按时间桶的序列、按维度分解。

只读接口，任何能读运行记录的调用方都能看。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_principal, require_permission
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.security import READ, Principal
from app.db.session import get_db
from app.schemas.dto import (
    ErrorEnvelope,
    UsageBreakdownRead,
    UsageBucketRead,
    UsageOverviewRead,
)
from app.services import usage
from app.services.usage import BUCKETS, DIMENSIONS, MAX_DAYS

_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "未认证"},
    403: {"model": ErrorEnvelope, "description": "无权限"},
}

router = APIRouter(prefix="/api/v1", tags=["usage"], dependencies=[Depends(get_principal)])


def _bucket_read(item: usage.UsageBucket) -> UsageBucketRead:
    return UsageBucketRead(
        key=item.key,
        runs=item.runs,
        succeeded=item.succeeded,
        failed=item.failed,
        running=item.running,
        tool_calls=item.tool_calls,
        prompt_chars=item.prompt_chars,
        answer_chars=item.answer_chars,
        errors=item.errors,
        avg_duration_ms=item.avg_duration_ms,
    )


@router.get("/usage/overview", response_model=UsageOverviewRead, responses=_ERRORS)
def overview(
    days: int = Query(30, ge=1, le=MAX_DAYS),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    """窗口内的总量指标。"""
    total = usage.overview(db, days=days)
    agents, models, tools = usage.active_dimensions(db, days=days)
    return UsageOverviewRead(
        days=days,
        runs=total.runs,
        succeeded=total.succeeded,
        failed=total.failed,
        running=total.running,
        tool_calls=total.tool_calls,
        prompt_chars=total.prompt_chars,
        answer_chars=total.answer_chars,
        errors=total.errors,
        avg_duration_ms=total.avg_duration_ms,
        agents=agents,
        models=models,
        tools=tools,
        daily_run_quota=settings.daily_run_quota,
    )


@router.get("/usage/timeline", response_model=list[UsageBucketRead], responses=_ERRORS)
def timeline(
    days: int = Query(30, ge=1, le=MAX_DAYS),
    bucket: str = Query("day"),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    """按时间桶的序列；没有数据的天也会出现（补零），方便直接画折线。"""
    if bucket not in BUCKETS:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"bucket 只能是 {'/'.join(BUCKETS)}",
            status_code=422,
        )
    return [_bucket_read(item) for item in usage.timeline(db, days=days, bucket=bucket)]


@router.get("/usage/breakdown", response_model=list[UsageBreakdownRead], responses=_ERRORS)
def breakdown(
    days: int = Query(30, ge=1, le=MAX_DAYS),
    dimension: str = Query("agent"),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    """按 Agent / 模型 / 工具分解。"""
    if dimension not in DIMENSIONS:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"dimension 只能是 {'/'.join(DIMENSIONS)}",
            status_code=422,
        )
    items = usage.breakdown(db, days=days, dimension=dimension)

    labels: dict[str, str] = {}
    if dimension == "agent":
        ids = {item.key for item in items if not item.key.startswith("(")}
        labels = usage.agent_labels(db, ids)

    return [
        UsageBreakdownRead(
            **_bucket_read(item).model_dump(),
            label=labels.get(item.key, item.key),
        )
        for item in items
    ]

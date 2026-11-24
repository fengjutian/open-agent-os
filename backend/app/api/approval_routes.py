"""人工审批（HITL）：批准或拒绝待执行的工具调用。

批准 → 执行该工具，把结果回灌并从检查点续跑**同一个 run**；
拒绝 → 不执行，把"被拒绝"作为工具结果回灌，让模型自行收尾。

两个端点都可能返回 202（又有工具需要审批），因此响应模型是联合类型。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, principal_actor, require_permission
from app.api.helpers import get_or_404
from app.core.errors import AppError, ErrorCode
from app.core.security import READ, WRITE_AGENTS, Principal
from app.db.session import get_db
from app.models.entities import Run, ToolApproval, now_utc
from app.schemas.dto import (
    ApprovalDecision,
    ApprovalPending,
    ErrorEnvelope,
    Page,
    SendMessageResponse,
    ToolApprovalRead,
)
from app.services.agent_runtime import AgentRuntime
from app.services.approvals import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    ApprovalRequired,
)
from app.services.audit import AuditService
from app.services.runs import STATUS_WAITING_APPROVAL
from app.services.tool_gateway import ToolGateway

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "未认证"},
    403: {"model": ErrorEnvelope, "description": "无权限"},
    404: {"model": ErrorEnvelope, "description": "资源不存在"},
    409: {"model": ErrorEnvelope, "description": "审批已处理或运行状态不匹配"},
}

router = APIRouter(prefix="/api/v1", tags=["approvals"], dependencies=[Depends(get_principal)])


@router.get("/approvals", response_model=Page[ToolApprovalRead], responses=_ERRORS)
def list_approvals(
    approval_status: str | None = Query(default=APPROVAL_PENDING, alias="status"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    query = select(ToolApproval)
    counter = select(func.count()).select_from(ToolApproval)
    if approval_status:
        query = query.where(ToolApproval.status == approval_status)
        counter = counter.where(ToolApproval.status == approval_status)

    total = db.scalar(counter) or 0
    items = db.scalars(
        query.order_by(ToolApproval.created_at.desc(), ToolApproval.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[ToolApprovalRead](items=items, total=total, limit=limit, offset=offset)


def _load_pending(db: Session, approval_id: str) -> tuple[ToolApproval, Run]:
    approval = get_or_404(db, ToolApproval, approval_id, "Approval")
    if approval.status != APPROVAL_PENDING:
        raise AppError(ErrorCode.CONFLICT, "该审批已被处理", status_code=409)

    run = db.get(Run, approval.run_id)
    if run is None or run.status != STATUS_WAITING_APPROVAL:
        raise AppError(ErrorCode.CONFLICT, "运行不在等待审批状态", status_code=409)
    return approval, run


def _decide(db: Session, approval: ToolApproval, *, status: str, actor: str, reason: str | None):
    approval.status = status
    approval.decided_by = actor
    approval.reason = reason
    approval.decided_at = now_utc()


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=SendMessageResponse | ApprovalPending,
    responses={**_ERRORS, 202: {"model": ApprovalPending, "description": "又有工具需要审批"}},
)
async def approve(
    approval_id: str,
    payload: ApprovalDecision,
    response: Response,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    approval, run = _load_pending(db, approval_id)
    actor = principal_actor(principal)

    # 先落审批结论：即使随后执行失败，也不能让它被再次批准
    _decide(db, approval, status=APPROVAL_APPROVED, actor=actor, reason=payload.reason)
    AuditService(db).record(
        action="tool.approval.approved",
        actor=actor,
        resource=f"tool:{approval.tool_name}",
        decision="allow",
        reason="人工批准",
        detail={"run_id": approval.run_id, "approval_id": approval.id},
    )
    db.commit()

    outcome = await ToolGateway(db).invoke(
        run_id=run.id,
        tool_name=approval.tool_name,
        arguments=approval.arguments,
        agent_id=approval.agent_id,
        conversation_id=approval.conversation_id,
        approved=True,
        # 复用暂停时那条 invocation，别让一次逻辑调用在记录里出现两行
        invocation_id=approval.invocation_id,
    )

    try:
        user_message, assistant_message = await AgentRuntime().resume(
            db,
            run,
            decision="approved",
            actor=actor,
            outcome=outcome,
            approved_call_id=approval.call_id,
        )
    except ApprovalRequired as exc:
        db.commit()
        response.status_code = status.HTTP_202_ACCEPTED
        return ApprovalPending(run_id=exc.run_id, approval_id=exc.approval_id, tool=exc.tool_name)

    return SendMessageResponse(
        run_id=run.id,
        user_message=user_message,
        assistant_message=assistant_message,
    )


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=SendMessageResponse | ApprovalPending,
    responses={**_ERRORS, 202: {"model": ApprovalPending, "description": "又有工具需要审批"}},
)
async def reject(
    approval_id: str,
    payload: ApprovalDecision,
    response: Response,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    approval, run = _load_pending(db, approval_id)
    actor = principal_actor(principal)

    _decide(db, approval, status=APPROVAL_REJECTED, actor=actor, reason=payload.reason)
    AuditService(db).record(
        action="tool.approval.rejected",
        actor=actor,
        resource=f"tool:{approval.tool_name}",
        decision="deny",
        reason=payload.reason or "人工拒绝",
        detail={"run_id": approval.run_id, "approval_id": approval.id},
    )
    db.commit()

    try:
        user_message, assistant_message = await AgentRuntime().resume(
            db,
            run,
            decision="rejected",
            actor=actor,
            reason=payload.reason,
        )
    except ApprovalRequired as exc:
        db.commit()
        response.status_code = status.HTTP_202_ACCEPTED
        return ApprovalPending(run_id=exc.run_id, approval_id=exc.approval_id, tool=exc.tool_name)

    return SendMessageResponse(
        run_id=run.id,
        user_message=user_message,
        assistant_message=assistant_message,
    )

"""V0.2 工具治理 API：注册表、策略规则、受控调用与审计查询。

治理结论在响应体中表达（``status`` / ``allowed``），HTTP 状态码只表示请求本身是否被处理——
被策略拒绝不是 API 错误，而是一次成功的治理决策。
"""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.helpers import count_rows, get_or_404
from app.core.errors import AppError, ErrorCode
from app.core.request_context import run_id_var
from app.db.session import get_db
from app.models.entities import Agent, AuditLog, Conversation, PolicyRule, ToolDefinition, ToolInvocation
from app.schemas.dto import (
    AgentToolRead,
    AuditLogRead,
    ErrorEnvelope,
    Page,
    PolicyRuleCreate,
    PolicyRuleRead,
    ToolCreate,
    ToolInvocationRead,
    ToolInvokeRequest,
    ToolInvokeResponse,
    ToolRead,
    ToolUpdate,
)
from app.services.audit import AuditService
from app.services.policy import PolicyEngine
from app.services.tool_gateway import ToolGateway
from app.services.tool_registry import ToolRegistry, assert_valid_schema

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_TOOL_ERRORS = {
    404: {"model": ErrorEnvelope, "description": "资源不存在"},
    409: {"model": ErrorEnvelope, "description": "资源冲突"},
}
_INVOKE_ERRORS = {
    **_TOOL_ERRORS,
    501: {"model": ErrorEnvelope, "description": "执行器不可用"},
    504: {"model": ErrorEnvelope, "description": "工具执行超时"},
}

router = APIRouter(prefix="/api/v1", tags=["tools"])


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------


@router.post("/tools", response_model=ToolRead, status_code=status.HTTP_201_CREATED, responses=_TOOL_ERRORS)
def create_tool(payload: ToolCreate, db: Session = Depends(get_db)):
    registry = ToolRegistry(db)
    if registry.get_by_name(payload.name) is not None:
        raise AppError(ErrorCode.CONFLICT, f"工具 {payload.name} 已存在", status_code=409)

    # 注册时就校验 schema 本身合法，避免把坏契约存进库。
    assert_valid_schema(payload.input_schema)

    tool = ToolDefinition(**payload.model_dump())
    db.add(tool)
    AuditService(db).record(
        action="tool.register",
        actor="system",
        resource=f"tool:{payload.name}",
        decision="allow",
        reason="注册工具定义",
        detail={
            "execution_type": payload.execution_type,
            "permissions": payload.permissions,
            "timeout_seconds": payload.timeout_seconds,
        },
    )
    db.commit()
    db.refresh(tool)
    return tool


@router.get("/tools", response_model=Page[ToolRead])
def list_tools(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    total = count_rows(db, ToolDefinition)
    items = db.scalars(
        select(ToolDefinition).order_by(ToolDefinition.name).limit(limit).offset(offset)
    ).all()
    return Page[ToolRead](items=items, total=total, limit=limit, offset=offset)


@router.get("/tools/{tool_id}", response_model=ToolRead, responses=_TOOL_ERRORS)
def get_tool(tool_id: str, db: Session = Depends(get_db)):
    return get_or_404(db, ToolDefinition, tool_id, "Tool")


@router.patch("/tools/{tool_id}", response_model=ToolRead, responses=_TOOL_ERRORS)
def update_tool(tool_id: str, payload: ToolUpdate, db: Session = Depends(get_db)):
    tool = get_or_404(db, ToolDefinition, tool_id, "Tool")
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(tool, key, value)
    AuditService(db).record(
        action="tool.update",
        actor="system",
        resource=f"tool:{tool.name}",
        decision="allow",
        reason="更新工具定义",
        detail={"changed_fields": sorted(changes)},
    )
    db.commit()
    db.refresh(tool)
    return tool


# ---------------------------------------------------------------------------
# 策略规则
# ---------------------------------------------------------------------------


@router.post(
    "/policy-rules",
    response_model=PolicyRuleRead,
    status_code=status.HTTP_201_CREATED,
    responses=_TOOL_ERRORS,
)
def create_policy_rule(payload: PolicyRuleCreate, db: Session = Depends(get_db)):
    if payload.agent_id is not None:
        get_or_404(db, Agent, payload.agent_id, "Agent")

    rule = PolicyRule(**payload.model_dump())
    db.add(rule)
    AuditService(db).record(
        action="policy.create",
        actor="system",
        resource=f"tool:{payload.tool_name}",
        decision=payload.effect,
        reason=payload.note or "创建策略规则",
        detail={
            "agent_id": payload.agent_id,
            "priority": payload.priority,
            "effect": payload.effect,
        },
    )
    db.commit()
    db.refresh(rule)
    return rule


@router.get("/policy-rules", response_model=Page[PolicyRuleRead])
def list_policy_rules(
    agent_id: str | None = Query(default=None),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    conditions = [PolicyRule.agent_id == agent_id] if agent_id else []
    total = count_rows(db, PolicyRule, *conditions)
    statement = select(PolicyRule)
    if conditions:
        statement = statement.where(*conditions)
    items = db.scalars(
        statement.order_by(PolicyRule.priority.desc(), PolicyRule.id).limit(limit).offset(offset)
    ).all()
    return Page[PolicyRuleRead](items=items, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Agent 视角的可用工具 + 受控调用
# ---------------------------------------------------------------------------


@router.get(
    "/agents/{agent_id}/tools",
    response_model=Page[AgentToolRead],
    responses=_TOOL_ERRORS,
)
def list_agent_tools(
    agent_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    get_or_404(db, Agent, agent_id, "Agent")

    total = count_rows(db, ToolDefinition)
    tools = db.scalars(
        select(ToolDefinition).order_by(ToolDefinition.name).limit(limit).offset(offset)
    ).all()

    engine = PolicyEngine(db)
    items = []
    for tool in tools:
        if not tool.enabled:
            items.append(
                AgentToolRead(
                    name=tool.name,
                    description=tool.description,
                    execution_type=tool.execution_type,
                    enabled=False,
                    allowed=False,
                    reason="工具已停用",
                )
            )
            continue
        decision = engine.evaluate(agent_id=agent_id, tool_name=tool.name)
        items.append(
            AgentToolRead(
                name=tool.name,
                description=tool.description,
                execution_type=tool.execution_type,
                enabled=True,
                allowed=decision.allowed,
                reason=decision.reason,
            )
        )
    return Page[AgentToolRead](items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/agents/{agent_id}/tools/{tool_name}/invoke",
    response_model=ToolInvokeResponse,
    responses=_INVOKE_ERRORS,
)
async def invoke_agent_tool(
    agent_id: str,
    tool_name: str,
    payload: ToolInvokeRequest,
    db: Session = Depends(get_db),
):
    get_or_404(db, Agent, agent_id, "Agent")
    if payload.conversation_id is not None:
        get_or_404(db, Conversation, payload.conversation_id, "Conversation")

    run_id = f"run_{uuid4().hex}"
    token = run_id_var.set(run_id)
    try:
        outcome = await ToolGateway(db).invoke(
            run_id=run_id,
            tool_name=tool_name,
            arguments=payload.arguments,
            agent_id=agent_id,
            conversation_id=payload.conversation_id,
        )
    finally:
        run_id_var.reset(token)

    return ToolInvokeResponse(
        run_id=run_id,
        invocation_id=outcome.invocation_id,
        tool_name=outcome.tool_name,
        status=outcome.status,
        allowed=outcome.allowed,
        reason=outcome.reason,
        result=outcome.result,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
    )


# ---------------------------------------------------------------------------
# 审计与调用记录
# ---------------------------------------------------------------------------


@router.get("/tool-invocations", response_model=Page[ToolInvocationRead])
def list_tool_invocations(
    run_id: str | None = Query(default=None),
    tool_name: str | None = Query(default=None),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    conditions = []
    if run_id:
        conditions.append(ToolInvocation.run_id == run_id)
    if tool_name:
        conditions.append(ToolInvocation.tool_name == tool_name)

    total = count_rows(db, ToolInvocation, *conditions)
    statement = select(ToolInvocation)
    if conditions:
        statement = statement.where(*conditions)
    items = db.scalars(
        statement.order_by(ToolInvocation.started_at.desc(), ToolInvocation.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[ToolInvocationRead](items=items, total=total, limit=limit, offset=offset)


@router.get("/audit-logs", response_model=Page[AuditLogRead])
def list_audit_logs(
    run_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    conditions = []
    if run_id:
        conditions.append(AuditLog.run_id == run_id)
    if action:
        conditions.append(AuditLog.action == action)

    total = count_rows(db, AuditLog, *conditions)
    statement = select(AuditLog)
    if conditions:
        statement = statement.where(*conditions)
    items = db.scalars(
        statement.order_by(AuditLog.created_at.desc(), AuditLog.id).limit(limit).offset(offset)
    ).all()
    return Page[AuditLogRead](items=items, total=total, limit=limit, offset=offset)

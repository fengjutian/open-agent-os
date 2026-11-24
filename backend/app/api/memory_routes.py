"""长期记忆：增删查与按需提炼。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, principal_actor, require_permission
from app.api.helpers import get_or_404
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.security import READ, WRITE_AGENTS, Principal
from app.db.session import get_db
from app.models.entities import Agent, Conversation, Memory, Message
from app.schemas.dto import ErrorEnvelope, MemoryCreate, MemoryRead, Page
from app.services.audit import AuditService
from app.services.memory import distill_conversation
from app.services.model_gateway import get_model_provider

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "未认证"},
    403: {"model": ErrorEnvelope, "description": "无权限"},
    404: {"model": ErrorEnvelope, "description": "资源不存在"},
    502: {"model": ErrorEnvelope, "description": "模型调用失败"},
}

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["memory"], dependencies=[Depends(get_principal)])


@router.post("/memories", response_model=MemoryRead, status_code=status.HTTP_201_CREATED, responses=_ERRORS)
def create_memory(
    payload: MemoryCreate,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    if payload.agent_id is not None:
        get_or_404(db, Agent, payload.agent_id, "Agent")

    memory = Memory(
        agent_id=payload.agent_id,
        content=payload.content.strip(),
        tags=payload.tags,
        source_conversation_id=payload.source_conversation_id,
    )
    db.add(memory)
    db.flush()
    AuditService(db).record(
        action="memory.create",
        actor=principal_actor(principal),
        resource=f"memory:{memory.id}",
        decision="allow",
        reason="写入长期记忆",
        detail={"agent_id": memory.agent_id, "tags": memory.tags},
    )
    db.commit()
    db.refresh(memory)
    return memory


@router.get("/memories", response_model=Page[MemoryRead], responses=_ERRORS)
def list_memories(
    q: str | None = Query(default=None, max_length=200),
    agent_id: str | None = Query(default=None),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    """列出记忆。`q` 是简单的包含匹配（检索打分在运行时里做）。"""
    statement = select(Memory)
    counter = select(func.count()).select_from(Memory)
    if agent_id:
        condition = or_(Memory.agent_id == agent_id, Memory.agent_id.is_(None))
        statement = statement.where(condition)
        counter = counter.where(condition)
    if q:
        condition = Memory.content.contains(q)
        statement = statement.where(condition)
        counter = counter.where(condition)

    total = db.scalar(counter) or 0
    items = db.scalars(
        statement.order_by(Memory.created_at.desc(), Memory.id).limit(limit).offset(offset)
    ).all()
    return Page[MemoryRead](items=items, total=total, limit=limit, offset=offset)


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
def delete_memory(
    memory_id: str,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    memory = get_or_404(db, Memory, memory_id, "Memory")
    AuditService(db).record(
        action="memory.delete",
        actor=principal_actor(principal),
        resource=f"memory:{memory.id}",
        decision="allow",
        reason="删除长期记忆",
        detail={"agent_id": memory.agent_id},
    )
    db.delete(memory)
    db.commit()
    return None


@router.post(
    "/conversations/{conversation_id}/memories/distill",
    response_model=list[MemoryRead],
    responses=_ERRORS,
)
async def distill(
    conversation_id: str,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    """把一段会话提炼成长期记忆。显式触发，不自动跑——避免每轮多一次模型调用。"""
    conversation = get_or_404(db, Conversation, conversation_id, "Conversation")
    agent = db.get(Agent, conversation.agent_id)
    if agent is None:
        raise AppError(ErrorCode.NOT_FOUND, "Agent not found", status_code=404)

    messages = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at)
        .limit(60)
    ).all()
    if len(messages) < 2:
        raise AppError(ErrorCode.VALIDATION_ERROR, "会话内容太少，无法提炼", status_code=422)

    transcript = "\n".join(f"{item.role}：{item.content}" for item in messages)
    provider = get_model_provider()
    try:
        extracted = await distill_conversation(
            provider,
            model=agent.model,
            transcript=transcript,
            limit=settings.memory_distill_limit,
        )
    except AppError as exc:
        raise AppError(exc.code, exc.message, status_code=502) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("memory distill failed")
        raise AppError(ErrorCode.MODEL_PROVIDER_ERROR, "提炼记忆失败", status_code=502) from exc

    created: list[Memory] = []
    for item in extracted:
        memory = Memory(
            agent_id=conversation.agent_id,
            content=item["content"],
            tags=item.get("tags") or [],
            source_conversation_id=conversation.id,
        )
        db.add(memory)
        created.append(memory)

    db.flush()
    AuditService(db).record(
        action="memory.distill",
        actor=principal_actor(principal),
        resource=f"conversation:{conversation.id}",
        decision="allow",
        reason="从会话提炼记忆",
        detail={"created": len(created)},
    )
    db.commit()
    for memory in created:
        db.refresh(memory)
    return created

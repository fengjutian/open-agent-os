import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, require_permission
from app.api.helpers import get_or_404
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.request_context import run_id_var
from app.core.security import READ, WRITE_AGENTS, Principal
from app.db.session import get_db
from app.models.entities import Agent, Conversation, Message
from app.schemas.dto import (
    AgentCreate,
    AgentRead,
    AgentUpdate,
    ConversationCreate,
    ConversationRead,
    ErrorEnvelope,
    MessageCreate,
    MessageRead,
    Page,
    SendMessageResponse,
)
from app.services.agent_runtime import AgentRuntime
from app.services.rate_limit import enforce_run_quota

logger = logging.getLogger(__name__)

# 让 /docs 上的错误契约与运行时一致（见 docs/api-contract.md）。
_COMMON_ERRORS = {
    422: {"model": ErrorEnvelope, "description": "请求参数校验失败"},
    500: {"model": ErrorEnvelope, "description": "服务器内部错误"},
}
_NOT_FOUND = {404: {"model": ErrorEnvelope, "description": "资源不存在"}}
_MODEL_ERRORS = {
    **_NOT_FOUND,
    424: {"model": ErrorEnvelope, "description": "模型未配置"},
    502: {"model": ErrorEnvelope, "description": "模型服务错误"},
    504: {"model": ErrorEnvelope, "description": "模型服务超时"},
}

router = APIRouter(prefix="/api/v1", responses=_COMMON_ERRORS, dependencies=[Depends(get_principal)])

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


@router.post("/agents", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
def create_agent(
    payload: AgentCreate,
    _principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    data = payload.model_dump()
    data["model"] = data["model"] or settings.model_name
    item = Agent(**data)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("/agents", response_model=Page[AgentRead])
def list_agents(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count()).select_from(Agent)) or 0
    items = db.scalars(
        # 用 id 兜底排序，保证分页边界稳定（created_at 可能相同）。
        select(Agent).order_by(Agent.created_at.desc(), Agent.id).limit(limit).offset(offset)
    ).all()
    return Page[AgentRead](items=items, total=total, limit=limit, offset=offset)


@router.get("/agents/{agent_id}", response_model=AgentRead, responses=_NOT_FOUND)
def get_agent(
    agent_id: str,
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    return get_or_404(db, Agent, agent_id, "Agent")


@router.patch("/agents/{agent_id}", response_model=AgentRead, responses=_NOT_FOUND)
def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    _principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    item = get_or_404(db, Agent, agent_id, "Agent")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    db.commit()
    db.refresh(item)
    return item


@router.delete(
    "/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_NOT_FOUND,
)
def delete_agent(
    agent_id: str,
    _principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    item = get_or_404(db, Agent, agent_id, "Agent")
    db.delete(item)
    db.commit()


@router.post(
    "/agents/{agent_id}/conversations",
    response_model=ConversationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(
    agent_id: str,
    payload: ConversationCreate,
    _principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    get_or_404(db, Agent, agent_id, "Agent")
    item = Conversation(agent_id=agent_id, title=payload.title)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get(
    "/agents/{agent_id}/conversations",
    response_model=Page[ConversationRead],
    responses=_NOT_FOUND,
)
def list_conversations(
    agent_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    get_or_404(db, Agent, agent_id, "Agent")
    total = (
        db.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.agent_id == agent_id)
        )
        or 0
    )
    items = db.scalars(
        select(Conversation)
        .where(Conversation.agent_id == agent_id)
        .order_by(Conversation.updated_at.desc(), Conversation.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[ConversationRead](items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=Page[MessageRead],
    responses=_NOT_FOUND,
)
def list_messages(
    conversation_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    get_or_404(db, Conversation, conversation_id, "Conversation")
    total = (
        db.scalar(
            select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
        )
        or 0
    )
    items = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc(), Message.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[MessageRead](items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=SendMessageResponse,
    responses=_MODEL_ERRORS,
)
async def send_message(
    conversation_id: str,
    payload: MessageCreate,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    conversation = get_or_404(db, Conversation, conversation_id, "Conversation")
    # 配额按密钥统计，避免单个 developer 无限制地消耗模型调用。
    enforce_run_quota(db, principal.key_id)

    run_id = f"run_{uuid4().hex}"
    token = run_id_var.set(run_id)
    try:
        user_message, assistant_message = await AgentRuntime().respond(db, conversation, payload.content)
    except AppError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.exception("agent run failed")
        raise AppError(ErrorCode.INTERNAL_ERROR, "Agent 执行失败", status_code=500) from exc
    finally:
        run_id_var.reset(token)

    return SendMessageResponse(
        run_id=run_id,
        user_message=user_message,
        assistant_message=assistant_message,
    )

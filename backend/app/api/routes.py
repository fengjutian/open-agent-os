import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, require_permission
from app.api.helpers import get_or_404
from app.api.sse import sse_event
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.request_context import principal_key_id_var, request_id_var, run_id_var
from app.core.security import READ, WRITE_AGENTS, Principal
from app.db.session import SessionLocal, get_db
from app.models.entities import Agent, Conversation, Message, RunEvent
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
from app.services.run_stream import get_run_hub
from app.services.skills import assert_skills_exist

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
    assert_skills_exist(db, payload.skill_names)
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
    if "skill_names" in payload.model_fields_set:
        assert_skills_exist(db, payload.skill_names)
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


@router.post(
    "/conversations/{conversation_id}/messages/stream",
    responses={
        **_MODEL_ERRORS,
        200: {
            "description": "SSE 事件流：run.started / delta / tool.call / tool.result / completed / error",
            "content": {"text/event-stream": {}},
        },
    },
)
async def stream_message(
    conversation_id: str,
    payload: MessageCreate,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    """以 SSE 流式返回本次运行。

    鉴权、配额、会话存在性在**建立流之前**校验，这样失败仍然是正常的 HTTP 错误码；
    一旦开始推送，就只能用 `error` 事件告知客户端。

    运行本身跑在独立任务里：客户端断开只退订，运行会继续跑完并落库，
    之后可以用 `/runs/{id}/events/stream` 回放结果。
    """
    get_or_404(db, Conversation, conversation_id, "Conversation")
    enforce_run_quota(db, principal.key_id)

    run_id = f"run_{uuid4().hex}"
    request_id = request_id_var.get()
    hub = get_run_hub()
    channel = hub.create(run_id)

    async def run_and_publish() -> None:
        run_token = run_id_var.set(run_id)
        principal_token = principal_key_id_var.set(principal.key_id)
        session = SessionLocal()
        try:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                channel.publish(
                    {"type": "error", "code": ErrorCode.NOT_FOUND, "message": "Conversation not found"}
                )
                return

            async for event in AgentRuntime().respond_stream(session, conversation, payload.content):
                if event.get("type") == "completed":
                    _persist_completed_event(session, run_id, event)
                channel.publish(event)
                # 增量文本不落库；其余事件逐条提交，重连方才能看到进展
                if event.get("type") != "delta":
                    session.commit()
        except Exception:
            logger.exception("stream run failed", extra={"fields": {"run_id": run_id}})
            session.rollback()
            channel.publish(
                {"type": "error", "code": ErrorCode.INTERNAL_ERROR, "message": "服务器内部错误"}
            )
        finally:
            session.close()
            channel.close()
            hub.discard(run_id)
            principal_key_id_var.reset(principal_token)
            run_id_var.reset(run_token)

    # 必须先订阅再启动任务：Mock provider 可能在 StreamingResponse 开始迭代前
    # 就跑完，届时事件会被发进「还不存在的订阅者」而丢失。队列本身是缓冲。
    queue = channel.subscribe()
    channel.task = asyncio.create_task(run_and_publish())

    async def event_stream():
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield sse_event(event)
        finally:
            # 客户端断开只会退订，不会取消运行任务
            channel.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 nginx 等反向代理的响应缓冲，否则会攒够一批才吐给客户端。
            "X-Accel-Buffering": "no",
            "X-Request-ID": request_id,
        },
    )


def _persist_completed_event(session: Session, run_id: str, event: dict) -> None:
    """把「完成」事件落库，重连方靠它拿到最终消息。"""
    # 必须先 flush：session 是 autoflush=False 的，否则 max(seq) 看不到
    # 本次运行刚追加、还没写盘的事件，新事件会复用同一个 seq。
    session.flush()
    next_seq = (
        session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)) or 0
    ) + 1
    session.add(RunEvent(run_id=run_id, seq=next_seq, type="completed", payload=event))

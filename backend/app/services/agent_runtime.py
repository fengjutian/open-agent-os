import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import log_event
from app.models.entities import Agent, Conversation, Message, now_utc
from app.services.context import ChatMessage, build_messages
from app.services.model_gateway import ModelProvider, get_model_provider

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Agent Loop 的唯一编排点：拼上下文 → 调模型 → 落库。

    provider 可注入，便于测试替换；默认按配置读取。
    """

    def __init__(self, provider: ModelProvider | None = None) -> None:
        self._provider = provider

    async def respond(
        self,
        db: Session,
        conversation: Conversation,
        user_content: str,
    ) -> tuple[Message, Message]:
        agent = db.get(Agent, conversation.agent_id)
        if agent is None:
            raise AppError(ErrorCode.NOT_FOUND, "Agent not found", status_code=404)

        user_created_at = now_utc()
        user_message = Message(
            conversation_id=conversation.id,
            role="user",
            content=user_content,
            created_at=user_created_at,
        )
        db.add(user_message)
        db.flush()

        history_rows = db.scalars(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.asc())
        ).all()
        history = [
            ChatMessage(role=m.role, content=m.content)
            for m in history_rows
            if m.role in {"user", "assistant"}
        ]

        context = build_messages(agent.system_prompt, history, settings.context_max_chars)
        if context.dropped_messages or context.truncated_newest:
            log_event(
                logger,
                "context trimmed to budget",
                conversation_id=conversation.id,
                history_messages=len(history),
                kept_messages=len(context.messages) - 1,
                dropped_messages=context.dropped_messages,
                truncated_newest=context.truncated_newest,
                used_chars=context.used_chars,
                max_chars=settings.context_max_chars,
            )

        log_event(
            logger,
            "agent run started",
            agent_id=agent.id,
            conversation_id=conversation.id,
            model=agent.model,
            context_messages=len(context.messages),
        )

        provider = self._provider or get_model_provider()
        answer = await provider.generate(
            model=agent.model,
            messages=[{"role": m.role, "content": m.content} for m in context.messages],
        )

        # 消息顺序依赖 created_at：Mock provider 几乎无耗时，两条消息可能落在同一微秒，
        # 导致用户消息与回复的顺序不稳定。这里强制让回复的时间戳严格晚于用户消息。
        assistant_created_at = now_utc()
        if assistant_created_at <= user_created_at:
            assistant_created_at = user_created_at + timedelta(microseconds=1)

        assistant_message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=answer,
            created_at=assistant_created_at,
        )
        db.add(assistant_message)
        # onupdate 只在 conversations 行本身被 UPDATE 时触发，而发消息只写 messages 表，
        # 因此必须显式推进 updated_at，否则会话列表按 updated_at 的排序永远反映不出新活动。
        conversation.updated_at = now_utc()
        db.commit()
        db.refresh(user_message)
        db.refresh(assistant_message)

        log_event(
            logger,
            "agent run completed",
            agent_id=agent.id,
            conversation_id=conversation.id,
            model=agent.model,
            answer_chars=len(answer),
        )
        return user_message, assistant_message

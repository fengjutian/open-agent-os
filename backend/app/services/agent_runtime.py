import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import log_event
from app.core.request_context import principal_key_id_var, run_id_var
from app.models.entities import Agent, Conversation, Message, Run, now_utc
from app.schemas.dto import MessageRead
from app.services.context import ChatMessage, ContextResult, build_messages
from app.services.model_gateway import (
    ModelProvider,
    ModelReply,
    StreamDone,
    TextDelta,
    ToolCall,
    get_model_provider,
)
from app.services.policy import PolicyEngine
from app.services.runs import STATUS_RUNNING, RunRecorder, finalize_failure
from app.services.skills import compose_system_prompt
from app.services.tool_gateway import ToolCallOutcome, ToolGateway
from app.services.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


def max_iterations_message(limit: int) -> str:
    return (
        "（已达到单次运行的工具调用轮次上限，未能给出最终回答。"
        f"上限为 {limit} 轮，请缩小任务范围后重试。）"
    )


@dataclass
class _RunState:
    """一次运行的共享状态：流式与非流式走同一套准备与收尾。"""

    agent: Agent
    run: Run
    recorder: RunRecorder
    user_message: Message
    user_created_at: datetime
    run_id: str
    fallback: dict[str, Any]
    context: ContextResult
    tool_specs: list[dict[str, Any]]
    tool_names: list[str]
    messages: list[dict[str, Any]]


class AgentRuntime:
    """Agent Loop 的唯一编排点：拼上下文 → 调模型 → 治理工具 → 落库。

    provider 可注入，便于测试替换；默认按配置读取。
    """

    def __init__(self, provider: ModelProvider | None = None) -> None:
        self._provider = provider

    # ------------------------------------------------------------------
    # 非流式
    # ------------------------------------------------------------------

    async def respond(
        self,
        db: Session,
        conversation: Conversation,
        user_content: str,
    ) -> tuple[Message, Message]:
        state = self._start(db, conversation, user_content)
        try:
            provider = self._provider or get_model_provider()
            answer = ""
            iterations = 0
            tool_call_count = 0

            for iteration in range(1, max(1, settings.max_tool_iterations) + 1):
                iterations = iteration
                state.recorder.event(
                    "model.request",
                    iteration=iteration,
                    message_count=len(state.messages),
                    tools=state.tool_names,
                )
                reply = await provider.generate(
                    model=state.agent.model,
                    messages=state.messages,
                    tools=state.tool_specs or None,
                )
                state.recorder.event(
                    "model.response",
                    iteration=iteration,
                    text_chars=len(reply.text),
                    tool_calls=[call.name for call in reply.tool_calls],
                )

                if not reply.tool_calls:
                    answer = reply.text
                    break

                state.messages.append(self._assistant_tool_call_message(reply))
                for call in reply.tool_calls:
                    tool_call_count += 1
                    outcome = await self._run_tool(db, state, call, iteration)
                    state.messages.append(self._tool_result_message(call, outcome))
            else:
                log_event(
                    logger,
                    "tool loop hit max iterations",
                    level=logging.WARNING,
                    max_iterations=settings.max_tool_iterations,
                )
                answer = max_iterations_message(max(1, settings.max_tool_iterations))

            return self._finish(
                db,
                state,
                answer=answer,
                iterations=iterations,
                tool_calls=tool_call_count,
            )
        except AppError as exc:
            self._fail(db, state, exc.code, exc.message)
            raise
        except Exception as exc:
            logger.exception("agent run crashed", extra={"fields": {"run_id": state.run_id}})
            self._fail(db, state, ErrorCode.INTERNAL_ERROR, type(exc).__name__)
            raise

    # ------------------------------------------------------------------
    # 流式
    # ------------------------------------------------------------------

    async def respond_stream(
        self,
        db: Session,
        conversation: Conversation,
        user_content: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """逐事件产出，最后落库。

        事件：run.started / delta / tool.call / tool.result / completed / error。
        与非流式共用 _start/_finish，保证两条路径的行为不会分叉。
        """
        state = self._start(db, conversation, user_content)
        try:
            yield {"type": "run.started", "run_id": state.run_id}

            provider = self._provider or get_model_provider()
            answer = ""
            iterations = 0
            tool_call_count = 0

            for iteration in range(1, max(1, settings.max_tool_iterations) + 1):
                iterations = iteration
                state.recorder.event(
                    "model.request",
                    iteration=iteration,
                    message_count=len(state.messages),
                    tools=state.tool_names,
                    streaming=True,
                )

                streamed: list[str] = []
                reply = ModelReply()
                async for event in provider.stream(
                    model=state.agent.model,
                    messages=state.messages,
                    tools=state.tool_specs or None,
                ):
                    if isinstance(event, TextDelta):
                        streamed.append(event.text)
                        yield {"type": "delta", "text": event.text}
                    elif isinstance(event, StreamDone):
                        reply = event.reply

                state.recorder.event(
                    "model.response",
                    iteration=iteration,
                    text_chars=len(reply.text),
                    tool_calls=[call.name for call in reply.tool_calls],
                )

                if not reply.tool_calls:
                    answer = reply.text or "".join(streamed)
                    break

                state.messages.append(self._assistant_tool_call_message(reply))
                for call in reply.tool_calls:
                    tool_call_count += 1
                    yield {"type": "tool.call", "tool": call.name, "arguments": call.arguments}
                    outcome = await self._run_tool(db, state, call, iteration)
                    yield {
                        "type": "tool.result",
                        "tool": call.name,
                        "status": outcome.status,
                        "error_code": outcome.error_code,
                        "result": outcome.result,
                    }
                    state.messages.append(self._tool_result_message(call, outcome))
            else:
                log_event(
                    logger,
                    "tool loop hit max iterations",
                    level=logging.WARNING,
                    max_iterations=settings.max_tool_iterations,
                )
                answer = max_iterations_message(max(1, settings.max_tool_iterations))

            user_message, assistant_message = self._finish(
                db,
                state,
                answer=answer,
                iterations=iterations,
                tool_calls=tool_call_count,
            )
            yield {
                "type": "completed",
                "run_id": state.run_id,
                "user_message": MessageRead.model_validate(user_message).model_dump(mode="json"),
                "assistant_message": MessageRead.model_validate(assistant_message).model_dump(mode="json"),
            }
        except AppError as exc:
            self._fail(db, state, exc.code, exc.message)
            yield {"type": "error", "code": exc.code, "message": exc.message}
        except GeneratorExit:
            # 客户端中途断开：把运行标记出来，避免永远停留在 running。
            self._fail(db, state, "client_disconnected", "客户端在流式中断开了连接")
            raise
        except Exception:
            logger.exception("agent stream crashed", extra={"fields": {"run_id": state.run_id}})
            self._fail(db, state, ErrorCode.INTERNAL_ERROR, "服务器内部错误")
            yield {"type": "error", "code": ErrorCode.INTERNAL_ERROR, "message": "服务器内部错误"}

    # ------------------------------------------------------------------
    # 共享的准备与收尾
    # ------------------------------------------------------------------

    def _start(self, db: Session, conversation: Conversation, user_content: str) -> _RunState:
        agent = db.get(Agent, conversation.agent_id)
        if agent is None:
            raise AppError(ErrorCode.NOT_FOUND, "Agent not found", status_code=404)

        run_id = run_id_var.get() or f"run_{uuid4().hex}"
        key_id = principal_key_id_var.get()
        fallback = {
            "agent_id": agent.id,
            "conversation_id": conversation.id,
            "key_id": key_id,
            "model": agent.model,
        }

        run = Run(id=run_id, status=STATUS_RUNNING, started_at=now_utc(), **fallback)
        db.add(run)
        db.flush()

        recorder = RunRecorder(db, run)
        system_prompt, applied_skills = compose_system_prompt(db, agent)
        recorder.event(
            "run.started",
            agent_id=agent.id,
            conversation_id=conversation.id,
            model=agent.model,
            skills=applied_skills,
        )

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
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc())
        ).all()
        history = [
            ChatMessage(role=m.role, content=m.content)
            for m in history_rows
            if m.role in {"user", "assistant"}
        ]

        context = build_messages(system_prompt, history, settings.context_max_chars)
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
            recorder.event(
                "context.trimmed",
                history_messages=len(history),
                dropped_messages=context.dropped_messages,
                truncated_newest=context.truncated_newest,
                used_chars=context.used_chars,
                max_chars=settings.context_max_chars,
            )

        # 只把「策略已放行」的工具暴露给模型：模型连看不到的工具都不会去请求。
        tool_specs = self._available_tool_specs(db, agent_id=agent.id)
        tool_names = [spec["function"]["name"] for spec in tool_specs]
        messages: list[dict[str, Any]] = [
            {"role": m.role, "content": m.content} for m in context.messages
        ]

        log_event(
            logger,
            "agent run started",
            agent_id=agent.id,
            conversation_id=conversation.id,
            model=agent.model,
            context_messages=len(context.messages),
            available_tools=tool_names,
        )

        return _RunState(
            agent=agent,
            run=run,
            recorder=recorder,
            user_message=user_message,
            user_created_at=user_created_at,
            run_id=run_id,
            fallback=fallback,
            context=context,
            tool_specs=tool_specs,
            tool_names=tool_names,
            messages=messages,
        )

    def _finish(
        self,
        db: Session,
        state: _RunState,
        *,
        answer: str,
        iterations: int,
        tool_calls: int,
    ) -> tuple[Message, Message]:
        # 消息顺序依赖 created_at：Mock provider 几乎无耗时，两条消息可能落在同一微秒，
        # 导致用户消息与回复的顺序不稳定。这里强制让回复的时间戳严格晚于用户消息。
        assistant_created_at = now_utc()
        if assistant_created_at <= state.user_created_at:
            assistant_created_at = state.user_created_at + timedelta(microseconds=1)

        assistant_message = Message(
            conversation_id=state.run.conversation_id,
            role="assistant",
            content=answer,
            created_at=assistant_created_at,
        )
        db.add(assistant_message)
        # onupdate 只在 conversations 行本身被 UPDATE 时触发，而发消息只写 messages 表，
        # 因此必须显式推进 updated_at，否则会话列表按 updated_at 的排序永远反映不出新活动。
        conversation = db.get(Conversation, state.run.conversation_id)
        if conversation is not None:
            conversation.updated_at = now_utc()

        state.recorder.succeed(
            iterations=iterations,
            tool_calls=tool_calls,
            answer_chars=len(answer),
            prompt_chars=state.context.used_chars,
        )
        db.commit()
        db.refresh(state.user_message)
        db.refresh(assistant_message)

        log_event(
            logger,
            "agent run completed",
            agent_id=state.agent.id,
            conversation_id=state.run.conversation_id,
            model=state.agent.model,
            answer_chars=len(answer),
            iterations=iterations,
            tool_calls=tool_calls,
        )
        return state.user_message, assistant_message

    @staticmethod
    def _fail(db: Session, state: _RunState, code: str, message: str) -> None:
        finalize_failure(
            db,
            run_id=state.run_id,
            error_code=code,
            error_message=message,
            fallback=state.fallback,
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _run_tool(
        self,
        db: Session,
        state: _RunState,
        call: ToolCall,
        iteration: int,
    ) -> ToolCallOutcome:
        outcome = await ToolGateway(db).invoke(
            run_id=state.run_id,
            tool_name=call.name,
            arguments=call.arguments,
            agent_id=state.agent.id,
            conversation_id=state.run.conversation_id,
        )
        state.recorder.event(
            "tool.result",
            iteration=iteration,
            tool=call.name,
            status=outcome.status,
            error_code=outcome.error_code,
        )
        return outcome

    @staticmethod
    def _available_tool_specs(db: Session, *, agent_id: str) -> list[dict[str, Any]]:
        engine = PolicyEngine(db)
        specs: list[dict[str, Any]] = []
        for tool in ToolRegistry(db).list_enabled():
            if not engine.evaluate(agent_id=agent_id, tool_name=tool.name).allowed:
                continue
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or tool.name,
                        "parameters": tool.input_schema
                        or {"type": "object", "additionalProperties": False},
                    },
                }
            )
        return specs

    @staticmethod
    def _assistant_tool_call_message(reply: ModelReply) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": reply.text or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in reply.tool_calls
            ],
        }

    @staticmethod
    def _tool_result_message(call: ToolCall, outcome: ToolCallOutcome) -> dict[str, Any]:
        # 把治理结论原样回灌给模型：被拒绝时它能看到原因并调整，而不是凭空成功。
        payload: dict[str, Any] = {"status": outcome.status}
        if outcome.result is not None:
            payload["result"] = outcome.result
        if outcome.error_code is not None:
            payload["error"] = {"code": outcome.error_code, "message": outcome.error_message}
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": json.dumps(payload, ensure_ascii=False),
        }

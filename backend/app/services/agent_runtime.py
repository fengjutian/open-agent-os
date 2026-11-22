import json
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import log_event
from app.core.request_context import principal_key_id_var, run_id_var
from app.models.entities import Agent, Conversation, Message, Run, now_utc
from app.services.context import ChatMessage, build_messages
from app.services.model_gateway import ModelProvider, ModelReply, ToolCall, get_model_provider
from app.services.policy import PolicyEngine
from app.services.runs import STATUS_RUNNING, RunRecorder, finalize_failure
from app.services.tool_gateway import ToolCallOutcome, ToolGateway
from app.services.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


def max_iterations_message(limit: int) -> str:
    return (
        "（已达到单次运行的工具调用轮次上限，未能给出最终回答。"
        f"上限为 {limit} 轮，请缩小任务范围后重试。）"
    )


class AgentRuntime:
    """Agent Loop 的唯一编排点：拼上下文 → 调模型 → 治理工具 → 落库。

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

        run_id = run_id_var.get() or f"run_{uuid4().hex}"
        key_id = principal_key_id_var.get()
        fallback = {
            "agent_id": agent.id,
            "conversation_id": conversation.id,
            "key_id": key_id,
            "model": agent.model,
        }

        run = Run(
            id=run_id,
            status=STATUS_RUNNING,
            started_at=now_utc(),
            **fallback,
        )
        db.add(run)
        db.flush()

        recorder = RunRecorder(db, run)
        recorder.event(
            "run.started",
            agent_id=agent.id,
            conversation_id=conversation.id,
            model=agent.model,
        )

        try:
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
                recorder.event(
                    "context.trimmed",
                    history_messages=len(history),
                    dropped_messages=context.dropped_messages,
                    truncated_newest=context.truncated_newest,
                    used_chars=context.used_chars,
                    max_chars=settings.context_max_chars,
                )

            provider = self._provider or get_model_provider()
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

            answer = ""
            iterations = 0
            tool_call_count = 0
            max_iterations = max(1, settings.max_tool_iterations)

            for iteration in range(1, max_iterations + 1):
                iterations = iteration
                recorder.event(
                    "model.request",
                    iteration=iteration,
                    message_count=len(messages),
                    tools=tool_names,
                )
                reply = await provider.generate(
                    model=agent.model,
                    messages=messages,
                    tools=tool_specs or None,
                )
                recorder.event(
                    "model.response",
                    iteration=iteration,
                    text_chars=len(reply.text),
                    tool_calls=[call.name for call in reply.tool_calls],
                )

                if not reply.tool_calls:
                    answer = reply.text
                    break

                messages.append(self._assistant_tool_call_message(reply))
                for call in reply.tool_calls:
                    tool_call_count += 1
                    outcome = await self._run_tool(db, agent, conversation, call, run_id)
                    recorder.event(
                        "tool.result",
                        iteration=iteration,
                        tool=call.name,
                        status=outcome.status,
                        error_code=outcome.error_code,
                    )
                    messages.append(self._tool_result_message(call, outcome))

                log_event(
                    logger,
                    "tool loop iteration",
                    iteration=iteration,
                    tool_calls=[call.name for call in reply.tool_calls],
                )
            else:
                # 达到轮次上限仍未收敛：明确告知，而不是无限循环或假装成功。
                log_event(
                    logger,
                    "tool loop hit max iterations",
                    level=logging.WARNING,
                    max_iterations=max_iterations,
                )
                answer = max_iterations_message(max_iterations)

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

            recorder.succeed(
                iterations=iterations,
                tool_calls=tool_call_count,
                answer_chars=len(answer),
                prompt_chars=context.used_chars,
            )
            db.commit()
            db.refresh(user_message)
            db.refresh(assistant_message)
        except AppError as exc:
            finalize_failure(
                db,
                run_id=run_id,
                error_code=exc.code,
                error_message=exc.message,
                fallback=fallback,
            )
            raise
        except Exception as exc:
            logger.exception("agent run crashed", extra={"fields": {"run_id": run_id}})
            finalize_failure(
                db,
                run_id=run_id,
                error_code=ErrorCode.INTERNAL_ERROR,
                error_message=type(exc).__name__,
                fallback=fallback,
            )
            raise

        log_event(
            logger,
            "agent run completed",
            agent_id=agent.id,
            conversation_id=conversation.id,
            model=agent.model,
            answer_chars=len(answer),
            iterations=iterations,
            tool_calls=tool_call_count,
        )
        return user_message, assistant_message

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

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

    async def _run_tool(
        self,
        db: Session,
        agent: Agent,
        conversation: Conversation,
        call: ToolCall,
        run_id: str,
    ) -> ToolCallOutcome:
        return await ToolGateway(db).invoke(
            run_id=run_id,
            tool_name=call.name,
            arguments=call.arguments,
            agent_id=agent.id,
            conversation_id=conversation.id,
        )

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

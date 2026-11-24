import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import log_event
from app.core.request_context import principal_key_id_var, run_id_var
from app.core.telemetry import record_run, span
from app.core.tenancy import current_tenant
from app.models.entities import (
    Agent,
    Conversation,
    Message,
    Run,
    ToolApproval,
    ToolInvocation,
    now_utc,
)
from app.schemas.dto import MessageRead
from app.services.approvals import STATUS_PENDING_APPROVAL, ApprovalRequired
from app.services.context import (
    _PER_MESSAGE_OVERHEAD,
    ChatMessage,
    ContextResult,
    build_messages,
)
from app.services.conversation_modes import filter_tool_specs, mode_hint, normalize_mode
from app.services.memory import retrieve_block
from app.services.model_gateway import (
    ModelProvider,
    ModelReply,
    StreamDone,
    TextDelta,
    ToolCall,
    get_model_provider,
)
from app.services.policy import PolicyEngine
from app.services.runs import (
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    RunRecorder,
    finalize_failure,
)
from app.services.skills import compose_system_prompt
from app.services.summarizer import summarize_history
from app.services.tool_gateway import ToolCallOutcome, ToolGateway
from app.services.tool_offload import (
    ARTIFACT_READ_TOOL_NAME,
    artifact_read_spec,
    build_envelope,
    offload_span,
    serialize_result,
    should_offload,
)
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
    # 从检查点续跑时的起始轮次（新运行为 1）
    start_iteration: int = 1


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
        *,
        mode: str | None = None,
    ) -> tuple[Message, Message]:
        """非流式入口：在外面套一层 span 与指标，主体逻辑在 _respond。"""
        started = time.perf_counter()
        outcome = "failed"
        with span("agent.run") as current:
            current.set_attribute("tenant.id", current_tenant())
            try:
                result = await self._respond(db, conversation, user_content, current, mode=mode)
                outcome = "succeeded"
                return result
            finally:
                current.set_attribute("run.status", outcome)
                record_run(
                    status=outcome, duration_ms=(time.perf_counter() - started) * 1000
                )

    async def _respond(
        self,
        db: Session,
        conversation: Conversation,
        user_content: str,
        current,
        *,
        mode: str | None = None,
    ) -> tuple[Message, Message]:
        state = await self._start(db, conversation, user_content, mode=mode)
        current.set_attribute("agent.id", state.agent.id)
        current.set_attribute("run.id", state.run_id)
        current.set_attribute("model", state.agent.model)
        try:
            answer, iterations, tool_call_count = await self._run_loop(db, state)
        except ApprovalRequired as exc:
            self._pause_for_approval(db, state, exc)
            raise
        except AppError as exc:
            self._fail(db, state, exc.code, exc.message)
            raise
        except Exception as exc:
            logger.exception("agent run crashed", extra={"fields": {"run_id": state.run_id}})
            self._fail(db, state, ErrorCode.INTERNAL_ERROR, type(exc).__name__)
            raise

        return self._finish(
            db,
            state,
            answer=answer,
            iterations=iterations,
            tool_calls=tool_call_count,
        )

    # ------------------------------------------------------------------
    # 流式
    # ------------------------------------------------------------------

    async def respond_stream(
        self,
        db: Session,
        conversation: Conversation,
        user_content: str,
        *,
        mode: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式入口：套一层 span 与指标，主体逻辑在 _respond_stream。"""
        started = time.perf_counter()
        outcome = "failed"
        with span("agent.run", **{"agent.stream": True}) as current:
            current.set_attribute("tenant.id", current_tenant())
            try:
                async for event in self._respond_stream(
                    db, conversation, user_content, current, mode=mode
                ):
                    if event.get("type") == "completed":
                        outcome = "succeeded"
                    yield event
            finally:
                current.set_attribute("run.status", outcome)
                record_run(
                    status=outcome, duration_ms=(time.perf_counter() - started) * 1000
                )

    async def _respond_stream(
        self,
        db: Session,
        conversation: Conversation,
        user_content: str,
        current,
        *,
        mode: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """逐事件产出，最后落库。

        事件：run.started / delta / tool.call / tool.result / completed / error。
        与非流式共用 _start/_finish，保证两条路径的行为不会分叉。
        """
        state = await self._start(db, conversation, user_content, mode=mode)
        current.set_attribute("agent.id", state.agent.id)
        current.set_attribute("run.id", state.run_id)
        current.set_attribute("model", state.agent.model)
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
                with span(
                    "model.request",
                    iteration=iteration,
                    model=state.agent.model,
                    streaming=True,
                ) as model_span:
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
                    model_span.set_attribute("reply.tool_calls", len(reply.tool_calls))

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
                    outcome, offloaded = await self._run_tool(db, state, call, iteration)
                    yield {
                        "type": "tool.result",
                        "tool": call.name,
                        "status": outcome.status,
                        "error_code": outcome.error_code,
                        # SSE 保持全量：帧不占上下文预算，调试时也能看到原始结果。
                        # 旁路只影响「给模型看什么」。
                        "result": outcome.result,
                    }
                    state.messages.append(self._tool_result_message(call, outcome, offloaded))
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
        except ApprovalRequired as exc:
            # 暂停而不是失败：保存检查点，把审批 id 告诉客户端，流到此结束
            self._pause_for_approval(db, state, exc)
            yield {
                "type": "run.waiting_approval",
                "run_id": state.run_id,
                "approval_id": exc.approval_id,
                "tool": exc.tool_name,
            }
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

    async def _start(
        self,
        db: Session,
        conversation: Conversation,
        user_content: str,
        *,
        mode: str | None = None,
    ) -> _RunState:
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
        # 单次请求可临时覆盖；否则用会话上的粘性设置；再否则回落默认值。
        resolved_mode = normalize_mode(mode or conversation.mode, default=settings.default_conversation_mode)

        system_prompt, applied_skills = compose_system_prompt(db, agent)
        hint = mode_hint(resolved_mode)
        if hint:
            system_prompt = f"{system_prompt}\n\n{hint}".strip()

        # 长期记忆：只注入与本次输入相关的条目；没有记忆时这里是空操作
        if settings.memory_enabled:
            memory_block = retrieve_block(
                db,
                user_content,
                agent_id=agent.id,
                limit=settings.memory_inject_limit,
            )
            if memory_block:
                system_prompt = f"{system_prompt}\n\n{memory_block}".strip()
                log_event(
                    logger,
                    "memory injected",
                    conversation_id=conversation.id,
                    memory_chars=len(memory_block),
                )

        recorder.event(
            "run.started",
            agent_id=agent.id,
            conversation_id=conversation.id,
            model=agent.model,
            skills=applied_skills,
            mode=resolved_mode,
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

        summary = await self._ensure_summary(db, conversation, agent, history, system_prompt)
        context = build_messages(
            system_prompt, history, settings.context_max_chars, summary=summary
        )
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
        # ask / plan 模式再收窄到只读工具，被裁掉的工具对模型而言不存在。
        all_specs = self._available_tool_specs(db, agent_id=agent.id)
        tool_specs = filter_tool_specs(
            all_specs,
            mode=resolved_mode,
            read_only_names=self._read_only_tool_names(db, agent_id=agent.id),
        )
        tool_names = [spec["function"]["name"] for spec in tool_specs]
        # 之前有旁路结果时，把 artifact.read 一起暴露给模型，
        # 这样它能在新一轮对话里把细节读回来。
        if self._conversation_has_offloaded(db, conversation_id=conversation.id):
            tool_specs = [*tool_specs, artifact_read_spec()]
            tool_names = [*tool_names, ARTIFACT_READ_TOOL_NAME]
        messages: list[dict[str, Any]] = [
            {"role": m.role, "content": m.content} for m in context.messages
        ]

        log_event(
            logger,
            "agent run started",
            agent_id=agent.id,
            conversation_id=conversation.id,
            model=agent.model,
            mode=resolved_mode,
            context_messages=len(context.messages),
            available_tools=tool_names,
            hidden_tools=len(all_specs) - len(tool_specs),
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

    async def _run_loop(self, db: Session, state: _RunState) -> tuple[str, int, int]:
        """模型 ⇄ 工具循环。需要审批时抛 ApprovalRequired，由调用方保存现场。"""
        provider = self._provider or get_model_provider()
        answer = ""
        iterations = state.start_iteration
        tool_call_count = 0

        for offset in range(max(1, settings.max_tool_iterations)):
            iteration = state.start_iteration + offset
            iterations = iteration
            state.recorder.event(
                "model.request",
                iteration=iteration,
                message_count=len(state.messages),
                tools=state.tool_names,
            )
            with span("model.request", iteration=iteration, model=state.agent.model) as model_span:
                reply = await provider.generate(
                    model=state.agent.model,
                    messages=state.messages,
                    tools=state.tool_specs or None,
                )
                model_span.set_attribute("reply.text_chars", len(reply.text))
                model_span.set_attribute("reply.tool_calls", len(reply.tool_calls))
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
                outcome, offloaded = await self._run_tool(db, state, call, iteration)
                state.messages.append(self._tool_result_message(call, outcome, offloaded))
        else:
            log_event(
                logger,
                "tool loop hit max iterations",
                level=logging.WARNING,
                max_iterations=settings.max_tool_iterations,
            )
            answer = max_iterations_message(max(1, settings.max_tool_iterations))

        return answer, iterations, tool_call_count

    def _pause_for_approval(self, db: Session, state: _RunState, exc: ApprovalRequired) -> None:
        """保存现场：检查点进数据库，进程重启后审批依然有效。"""
        state.run.status = STATUS_WAITING_APPROVAL
        state.run.checkpoint = state.messages
        state.run.checkpoint_iteration = exc.iteration
        state.run.finished_at = None
        state.recorder.event(
            "run.waiting_approval",
            approval_id=exc.approval_id,
            tool=exc.tool_name,
            iteration=exc.iteration,
        )
        db.commit()
        log_event(
            logger,
            "agent run paused for approval",
            run_id=state.run_id,
            tool=exc.tool_name,
            approval_id=exc.approval_id,
        )

    def _resume_state(self, db: Session, run: Run) -> _RunState:
        """从检查点还原运行状态。"""
        agent = db.get(Agent, run.agent_id) if run.agent_id else None
        conversation = db.get(Conversation, run.conversation_id) if run.conversation_id else None
        if agent is None or conversation is None:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "无法恢复运行：Agent 或会话已不存在",
                status_code=409,
            )

        messages = list(run.checkpoint or [])
        mode = normalize_mode(conversation.mode)
        tool_specs = filter_tool_specs(
            self._available_tool_specs(db, agent_id=agent.id),
            mode=mode,
            read_only_names=self._read_only_tool_names(db, agent_id=agent.id),
        )
        # 续跑时同样要把 artifact.read 补回来：检查点里可能有被旁路的结果
        if self._conversation_has_offloaded(db, conversation_id=conversation.id):
            tool_specs = [*tool_specs, artifact_read_spec()]
        user_message = db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.role == "user")
            .order_by(Message.created_at.desc())
            .limit(1)
        ).first()
        if user_message is None:
            raise AppError(ErrorCode.INTERNAL_ERROR, "无法恢复运行：缺少用户消息", status_code=409)

        return _RunState(
            agent=agent,
            run=run,
            recorder=RunRecorder(db, run),
            user_message=user_message,
            # SQLite 读回来是 naive，补成 UTC 再做时间比较
            user_created_at=(
                user_message.created_at
                if user_message.created_at.tzinfo
                else user_message.created_at.replace(tzinfo=UTC)
            ),
            run_id=run.id,
            fallback={},
            context=ContextResult(
                messages=[],
                used_chars=sum(len(str(m.get("content") or "")) for m in messages),
                dropped_messages=0,
                truncated_newest=False,
            ),
            tool_specs=tool_specs,
            tool_names=[spec["function"]["name"] for spec in tool_specs],
            messages=messages,
            start_iteration=(run.checkpoint_iteration or 0) + 1,
        )

    async def resume(
        self,
        db: Session,
        run: Run,
        *,
        decision: str,
        actor: str,
        outcome: ToolCallOutcome | None = None,
        approved_call_id: str | None = None,
        reason: str | None = None,
    ) -> tuple[Message, Message]:
        """审批结束后续跑同一个 run。

        检查点最后一条是模型的 assistant(tool_calls) 消息；这一批里可能不止一个调用，
        所以要把**每个**调用都补上工具结果，否则真实模型会拒绝（tool_calls 缺结果）。
        被批准的那个用传入的 outcome，其余重新走网关（可能再次触发审批）。
        """
        state = self._resume_state(db, run)
        pending = state.messages[-1] if state.messages else {}
        calls = [
            ToolCall(
                id=item["id"],
                name=item["function"]["name"],
                arguments=json.loads(item["function"]["arguments"] or "{}"),
            )
            for item in pending.get("tool_calls", [])
        ]
        if not calls:
            raise AppError(ErrorCode.INTERNAL_ERROR, "检查点里没有待处理工具调用", status_code=409)

        for call in calls:
            if outcome is not None and call.id == approved_call_id:
                resolved = outcome
            elif decision == "rejected":
                resolved = ToolCallOutcome(
                    invocation_id="",
                    tool_name=call.name,
                    status="rejected",
                    allowed=False,
                    reason="用户拒绝",
                    error_code="tool_rejected",
                    error_message=reason or "用户拒绝了该工具调用，请勿重试，直接给出你能给的最佳回答。",
                )
            else:
                resolved = await self._run_tool(db, state, call, state.start_iteration - 1)
            state.messages.append(self._tool_result_message(call, resolved))

        run.status = STATUS_RUNNING
        db.flush()

        try:
            answer, iterations, tool_calls = await self._run_loop(db, state)
        except ApprovalRequired as exc:
            self._pause_for_approval(db, state, exc)
            raise

        return self._finish(
            db, state, answer=answer, iterations=iterations, tool_calls=tool_calls
        )

    async def _ensure_summary(
        self,
        db: Session,
        conversation: Conversation,
        agent: Agent,
        history: list[ChatMessage],
        system_prompt: str,
    ) -> str | None:
        """历史装不下时，把最老的一段压成摘要。

        三个条件都不满足就直接返回已有摘要（可能是 None，即走原来的整条丢弃）：
        还没被覆盖的消息够多、总长度确实超过触发线、以及摘要本身没失败。
        """
        if not settings.context_summary_enabled or not history:
            return conversation.summary

        keep = max(0, settings.context_summary_keep_messages)
        already = conversation.summary_message_count or 0
        pending_end = len(history) - keep
        # 还没攒够一批就不摘要：否则每轮都要多调一次模型，得不偿失
        if pending_end - already < max(1, keep):
            return conversation.summary

        total = sum(len(message.content) + _PER_MESSAGE_OVERHEAD for message in history)
        if total + len(system_prompt) <= settings.context_max_chars * settings.context_summary_trigger_ratio:
            return conversation.summary

        provider = self._provider or get_model_provider()
        try:
            summary = await summarize_history(
                provider,
                model=agent.model,
                messages=history[already:pending_end],
                previous_summary=conversation.summary,
                max_chars=settings.context_summary_max_chars,
            )
        except Exception:  # noqa: BLE001 - 摘要失败必须退回截断，不能拖垮对话
            logger.warning(
                "summarize failed, falling back to truncation",
                exc_info=True,
                extra={"fields": {"conversation_id": conversation.id}},
            )
            return conversation.summary

        conversation.summary = summary
        conversation.summary_message_count = pending_end
        db.flush()
        log_event(
            logger,
            "history summarized",
            conversation_id=conversation.id,
            summarized_messages=pending_end - already,
            summary_chars=len(summary),
        )
        return summary

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
    ) -> tuple[ToolCallOutcome, str | None]:
        """执行一次工具调用。返回结果与「被旁路的结果文本」（未旁路时为 None）。"""
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
        if outcome.status == STATUS_PENDING_APPROVAL:
            # 不执行、不当作失败：记下调用 id 后交给上层保存检查点并暂停运行
            approval = db.get(ToolApproval, outcome.approval_id) if outcome.approval_id else None
            if approval is not None:
                approval.call_id = call.id
                db.flush()
            raise ApprovalRequired(
                run_id=state.run_id,
                approval_id=outcome.approval_id or "",
                tool_name=call.name,
                iteration=iteration,
            )
        offloaded = self._maybe_offload(db, state, outcome, iteration=iteration)
        return outcome, offloaded

    def _maybe_offload(
        self,
        db: Session,
        state: _RunState,
        outcome: ToolCallOutcome,
        *,
        iteration: int,
    ) -> str | None:
        """结果过大时把它从模型上下文里剥离，并告诉模型怎么取回。

        **数据没丢**：完整结果仍在 `ToolInvocation.result` 里，模型可以用
        `artifact.read` 按需取回。返回被旁路的结果文本（未旁路时返回 None）。
        """
        serialized = serialize_result(outcome.result)
        if serialized is None:
            return None
        if not should_offload(status=outcome.status, serialized=serialized):
            return None

        invocation = db.get(ToolInvocation, outcome.invocation_id)
        if invocation is not None:
            invocation.offloaded = True
            invocation.result_chars = len(serialized)
            db.flush()

        # 模型看不到 artifact.read 就不会用它取回细节——
        # 工具规格必须在旁路发生的那一刻就补上，下一轮请求里它才存在。
        self._expose_artifact_read(state)
        state.recorder.event(
            "tool.result.offloaded",
            iteration=iteration,
            tool=outcome.tool_name,
            chars=len(serialized),
        )
        offload_span(outcome.tool_name, len(serialized))
        log_event(
            logger,
            "tool result offloaded",
            tool=outcome.tool_name,
            chars=len(serialized),
            invocation_id=outcome.invocation_id,
        )
        return serialized

    @staticmethod
    def _expose_artifact_read(state: _RunState) -> None:
        """把 artifact.read 加进本次运行的工具规格（已存在则不动）。"""
        if any(
            spec["function"]["name"] == ARTIFACT_READ_TOOL_NAME
            for spec in state.tool_specs
        ):
            return
        state.tool_specs.append(artifact_read_spec())
        state.tool_names.append(ARTIFACT_READ_TOOL_NAME)

    @staticmethod
    def _conversation_has_offloaded(db: Session, *, conversation_id: str) -> bool:
        """这个会话里是否出现过被旁路的结果。"""
        return bool(
            db.scalar(
                select(func.count())
                .select_from(ToolInvocation)
                .where(
                    ToolInvocation.conversation_id == conversation_id,
                    ToolInvocation.offloaded.is_(True),
                )
            )
        )

    @staticmethod
    def _read_only_tool_names(db: Session, *, agent_id: str) -> set[str]:
        """被显式标记为只读、且策略放行的工具名。"""
        engine = PolicyEngine(db)
        return {
            tool.name
            for tool in ToolRegistry(db).list_enabled()
            if tool.read_only and engine.evaluate(agent_id=agent_id, tool_name=tool.name).allowed
        }

    @staticmethod
    def _available_tool_specs(db: Session, *, agent_id: str) -> list[dict[str, Any]]:
        engine = PolicyEngine(db)
        specs: list[dict[str, Any]] = []
        for tool in ToolRegistry(db).list_enabled():
            if tool.name == ARTIFACT_READ_TOOL_NAME:
                # 平台能力，不跟着注册表一起暴露：
                # 只有真的存在旁路结果时才让模型看见它（见 _expose_artifact_read）
                continue
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
    def _tool_result_message(
        call: ToolCall,
        outcome: ToolCallOutcome,
        offloaded: str | None = None,
    ) -> dict[str, Any]:
        # 把治理结论原样回灌给模型：被拒绝时它能看到原因并调整，而不是凭空成功。
        if offloaded is not None:
            # 结果过大：只给信封（状态 + 预览 + 引用），数据本体留在库里
            payload = build_envelope(
                invocation_id=outcome.invocation_id,
                serialized=offloaded,
            )
        else:
            payload: dict[str, Any] = {"status": outcome.status}
            if outcome.result is not None:
                payload["result"] = outcome.result
            if outcome.error_code is not None:
                payload["error"] = {
                    "code": outcome.error_code,
                    "message": outcome.error_message,
                }
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": json.dumps(payload, ensure_ascii=False),
        }

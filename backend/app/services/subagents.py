"""子代理：让一个 Agent 把子任务交给另一个 Agent 执行。

与普通工具的区别：它不是"调一个函数"，而是**派生一次完整的运行**——
有自己的会话、自己的 Run 记录、自己的上下文预算。主运行只拿到最终回答。

三条硬约束：

1. **必须换 run_id**。子运行若沿用父运行的 `run_id`，会直接撞 Run 主键。
2. **深度上限**。A 派 B、B 再派 C 是合理的，无限套娃不是。默认最多 2 层。
3. **目标 Agent 必须存在且同租户**。租户过滤由 ORM 事件保证，查不到就是 404。

安全边界：这实际上是"以另一个 Agent 的身份花模型额度"。调用方必须是本租户内
显式放行了 `spawn_subagent` 工具的人——策略与审计仍然生效。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.request_context import run_id_var
from app.models.entities import Agent, Conversation

logger = logging.getLogger(__name__)

SPAWN_TOOL_NAME = "spawn_subagent"

_depth_var: ContextVar[int] = ContextVar("subagent_depth", default=0)


def current_depth() -> int:
    return _depth_var.get()


def reset_depth(token) -> None:  # noqa: ANN001 - ContextVar token
    _depth_var.reset(token)


async def run_subagent(
    db: Session,
    *,
    arguments: dict[str, Any],
    parent_run_id: str,
) -> dict[str, Any]:
    """执行一次子运行，返回可回灌给主模型的工具结果。"""
    depth = current_depth()
    if depth >= max(1, settings.subagent_max_depth):
        raise AppError(
            ErrorCode.TOOL_EXECUTION_FAILED,
            f"子代理嵌套层级已达上限（{settings.subagent_max_depth}），拒绝继续派生",
            status_code=400,
        )

    task = str(arguments.get("task") or "").strip()
    if not task:
        raise AppError(ErrorCode.VALIDATION_ERROR, "子代理任务不能为空", status_code=422)

    agent_id = arguments.get("agent_id")
    agent = db.get(Agent, str(agent_id)) if agent_id else None
    if agent is None:
        raise AppError(ErrorCode.NOT_FOUND, "目标 Agent 不存在", status_code=404)

    conversation = Conversation(
        agent_id=agent.id,
        title=f"子任务：{task[:40]}",
    )
    db.add(conversation)
    db.flush()

    # 延迟导入：agent_runtime 也依赖网关，顶层导入会形成循环
    from app.services.agent_runtime import AgentRuntime

    # 子运行必须有自己的 run_id，否则会和父运行撞主键
    child_run_id = f"run_{uuid4().hex}"
    run_token = run_id_var.set(child_run_id)
    depth_token = _depth_var.set(depth + 1)
    try:
        _, answer = await AgentRuntime().respond(db, conversation, task)
    finally:
        reset_depth(depth_token)
        run_id_var.reset(run_token)

    logger.info(
        "subagent finished",
        extra={
            "fields": {
                "parent_run_id": parent_run_id,
                "child_run_id": child_run_id,
                "agent_id": agent.id,
                "depth": depth + 1,
            }
        },
    )
    return {
        "answer": answer.content,
        "agent_id": agent.id,
        "agent_name": agent.name,
        "conversation_id": conversation.id,
        "run_id": child_run_id,
        "depth": depth + 1,
    }

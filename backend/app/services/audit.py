"""审计服务。

审计记录不挂外键、不随业务数据级联删除，目的是「发生了什么」可追溯。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.request_context import request_id_var, run_id_var
from app.models.entities import AuditLog


class AuditService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def record(
        self,
        *,
        action: str,
        actor: str,
        resource: str,
        decision: str | None = None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            run_id=run_id or run_id_var.get(),
            # request_id 来自上下文中间件；直接调用（如脚本）时为默认值 "-"，这里归一为 None。
            request_id=None if (rid := request_id_var.get()) in (None, "-") else rid,
            actor=actor,
            action=action,
            resource=resource,
            decision=decision,
            reason=reason,
            detail=detail,
        )
        self._db.add(entry)
        return entry


def agent_actor(agent_id: str | None) -> str:
    return f"agent:{agent_id}" if agent_id else "system"

"""审计服务。

审计记录不挂外键、不随业务数据级联删除，目的是「发生了什么」可追溯。
每条记录都会被链进哈希链（见 app/services/audit_chain.py），因此改写或删除会被发现。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.request_context import request_id_var, run_id_var
from app.core.tenancy import current_tenant
from app.models.entities import AuditLog, now_utc
from app.services.audit_chain import link_entry


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
            # id / tenant_id / created_at 都显式赋值：哈希需要它们，而列默认值要到 flush
            # 才生效，晚于计算。
            id=str(uuid4()),
            tenant_id=current_tenant(),
            run_id=run_id or run_id_var.get(),
            # request_id 来自上下文中间件；直接调用（如脚本）时为默认值 "-"，这里归一为 None。
            request_id=None if (rid := request_id_var.get()) in (None, "-") else rid,
            actor=actor,
            action=action,
            resource=resource,
            decision=decision,
            reason=reason,
            detail=detail,
            created_at=now_utc(),
        )
        # 必须在 add 之前链接：加入 session 后查询会 autoflush，此时 seq 仍为 NULL。
        link_entry(self._db, entry)
        self._db.add(entry)
        return entry


def agent_actor(agent_id: str | None) -> str:
    return f"agent:{agent_id}" if agent_id else "system"

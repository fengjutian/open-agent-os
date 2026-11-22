"""租户上下文。

租户信息随请求走 contextvar，再由 `app/db/tenancy.py` 的 ORM 事件统一施加过滤。
这样「忘记加 where」不会是漏隔离的原因——过滤发生在 SQLAlchemy 层，而不是每个查询里。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

DEFAULT_TENANT = "default"

tenant_id_var: ContextVar[str] = ContextVar("tenant_id", default=DEFAULT_TENANT)


def current_tenant() -> str:
    return tenant_id_var.get() or DEFAULT_TENANT


def set_tenant(tenant_id: str | None) -> Token[str]:
    return tenant_id_var.set(tenant_id or DEFAULT_TENANT)


def reset_tenant(token: Token[str]) -> None:
    tenant_id_var.reset(token)

"""租户与成员关系。

职责边界：

- `Tenant` 是隔离边界本身，停用后该租户的所有请求立即被拒；
- `Membership` 把「身份 → 租户内角色」落库，使角色不必完全信任 IdP 的 claim；
- API Key 自带角色，不需要 membership（它是服务凭据）。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.security import ROLES, Principal, principal_for
from app.core.tenancy import DEFAULT_TENANT, current_tenant
from app.models.entities import Membership, Tenant

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"


def get_tenant(db: Session, tenant_id: str) -> Tenant | None:
    return db.get(Tenant, tenant_id)


def ensure_tenant_exists(db: Session, tenant_id: str, *, name: str | None = None) -> Tenant:
    """确保租户存在；不存在则创建（用于默认租户与引导流程）。"""
    tenant = get_tenant(db, tenant_id)
    if tenant is not None:
        return tenant

    tenant = Tenant(id=tenant_id, name=name or tenant_id, status=STATUS_ACTIVE)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def assert_tenant_active(db: Session, tenant_id: str) -> Tenant:
    tenant = get_tenant(db, tenant_id)
    if tenant is None:
        # 不给「是否存在」的额外信息，统一按无权限处理
        raise AppError(ErrorCode.FORBIDDEN, "租户不存在或已停用", status_code=403)
    if tenant.status != STATUS_ACTIVE:
        raise AppError(ErrorCode.FORBIDDEN, "租户已停用", status_code=403)
    return tenant


def resolve_membership(db: Session, principal: Principal) -> Principal:
    """用成员关系覆盖角色。

    - 有 membership → 以 membership 的角色为准（平台可以覆盖 IdP 的判断）；
    - 没有 membership → 默认沿用凭据里的角色；若开启强制成员制则拒绝。
    """
    membership = db.scalar(
        select(Membership).where(Membership.subject == principal.key_id)
    )
    if membership is not None:
        if membership.role != principal.role:
            logger.info(
                "membership overrides role",
                extra={
                    "fields": {
                        "subject": principal.key_id,
                        "claimed": principal.role,
                        "effective": membership.role,
                    }
                },
            )
        return principal_for(
            key_id=principal.key_id,
            name=principal.name,
            role=membership.role,
            auth_method=principal.auth_method,
            tenant_id=principal.tenant_id,
        )

    if principal.auth_method == "oidc" and settings.oidc_require_membership:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "你尚未加入该租户，请联系管理员添加成员",
            status_code=403,
        )
    return principal


def create_membership(db: Session, *, subject: str, role: str) -> Membership:
    if role not in ROLES:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"未知角色 {role}", status_code=422)
    if db.scalar(select(Membership).where(Membership.subject == subject)) is not None:
        raise AppError(ErrorCode.CONFLICT, f"成员 {subject} 已存在", status_code=409)

    membership = Membership(subject=subject, role=role, tenant_id=current_tenant())
    db.add(membership)
    db.flush()
    return membership


def bootstrap_default_tenant(db: Session) -> None:
    """保证默认租户存在，避免引导阶段因为没有租户而拒绝。"""
    ensure_tenant_exists(db, DEFAULT_TENANT, name="Default")

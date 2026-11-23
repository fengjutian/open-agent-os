"""租户与成员管理。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, principal_actor, require_permission
from app.api.helpers import count_rows, get_or_404
from app.core.errors import AppError, ErrorCode
from app.core.security import READ, WRITE_MEMBERS, Principal
from app.db.session import get_db
from app.models.entities import Membership, Tenant
from app.schemas.dto import (
    ErrorEnvelope,
    MembershipCreate,
    MembershipRead,
    MembershipUpdate,
    Page,
    TenantCreate,
    TenantRead,
)
from app.services.audit import AuditService
from app.services.tenants import STATUS_ACTIVE, create_membership, get_tenant

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "未认证"},
    403: {"model": ErrorEnvelope, "description": "无权限"},
    404: {"model": ErrorEnvelope, "description": "资源不存在"},
    409: {"model": ErrorEnvelope, "description": "资源冲突"},
}

router = APIRouter(prefix="/api/v1", tags=["tenants"], dependencies=[Depends(get_principal)])


@router.post(
    "/tenants",
    response_model=TenantRead,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def create_tenant(
    payload: TenantCreate,
    principal: Principal = Depends(require_permission(WRITE_MEMBERS)),
    db: Session = Depends(get_db),
):
    if get_tenant(db, payload.id) is not None:
        raise AppError(ErrorCode.CONFLICT, f"租户 {payload.id} 已存在", status_code=409)

    tenant = Tenant(id=payload.id, name=payload.name, status=STATUS_ACTIVE)
    db.add(tenant)
    AuditService(db).record(
        action="tenant.create",
        actor=principal_actor(principal),
        resource=f"tenant:{payload.id}",
        decision="allow",
        reason="创建租户",
    )
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/tenants/current", response_model=TenantRead, responses=_ERRORS)
def get_current_tenant(
    principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    tenant = get_tenant(db, principal.tenant_id)
    if tenant is None:
        raise AppError(ErrorCode.NOT_FOUND, "租户不存在", status_code=404)
    return tenant


@router.get("/memberships", response_model=Page[MembershipRead])
def list_memberships(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    total = count_rows(db, Membership)
    items = db.scalars(
        select(Membership).order_by(Membership.created_at, Membership.id).limit(limit).offset(offset)
    ).all()
    return Page[MembershipRead](items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/memberships",
    response_model=MembershipRead,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def add_membership(
    payload: MembershipCreate,
    principal: Principal = Depends(require_permission(WRITE_MEMBERS)),
    db: Session = Depends(get_db),
):
    membership = create_membership(db, subject=payload.subject, role=payload.role)
    AuditService(db).record(
        action="membership.create",
        actor=principal_actor(principal),
        resource=f"membership:{payload.subject}",
        decision="allow",
        reason=f"加入租户，角色 {payload.role}",
        detail={"role": payload.role},
    )
    db.commit()
    db.refresh(membership)
    return membership


@router.patch("/memberships/{membership_id}", response_model=MembershipRead, responses=_ERRORS)
def update_membership(
    membership_id: str,
    payload: MembershipUpdate,
    principal: Principal = Depends(require_permission(WRITE_MEMBERS)),
    db: Session = Depends(get_db),
):
    membership = get_or_404(db, Membership, membership_id, "Membership")
    previous = membership.role
    membership.role = payload.role
    AuditService(db).record(
        action="membership.update",
        actor=principal_actor(principal),
        resource=f"membership:{membership.subject}",
        decision="allow",
        reason=f"角色 {previous} → {payload.role}",
        detail={"from": previous, "to": payload.role},
    )
    db.commit()
    db.refresh(membership)
    return membership


@router.delete(
    "/memberships/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
)
def remove_membership(
    membership_id: str,
    principal: Principal = Depends(require_permission(WRITE_MEMBERS)),
    db: Session = Depends(get_db),
):
    membership = get_or_404(db, Membership, membership_id, "Membership")
    subject = membership.subject
    db.delete(membership)
    AuditService(db).record(
        action="membership.delete",
        actor=principal_actor(principal),
        resource=f"membership:{subject}",
        decision="allow",
        reason="移出租户",
    )
    db.commit()

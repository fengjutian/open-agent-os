"""API Key 管理。全部端点都要求 write:keys 权限（即仅 admin）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import principal_actor, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.security import WRITE_KEYS, Principal
from app.db.session import get_db
from app.schemas.dto import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyRead,
    ApiKeyUpdate,
    ErrorEnvelope,
    Page,
)
from app.services.api_keys import ApiKeyService
from app.services.audit import AuditService

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "未认证"},
    403: {"model": ErrorEnvelope, "description": "无权限"},
    404: {"model": ErrorEnvelope, "description": "资源不存在"},
    409: {"model": ErrorEnvelope, "description": "资源冲突"},
}

router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def create_api_key(
    payload: ApiKeyCreate,
    principal: Principal = Depends(require_permission(WRITE_KEYS)),
    db: Session = Depends(get_db),
):
    record, raw = ApiKeyService(db).create(name=payload.name, role=payload.role)
    AuditService(db).record(
        action="apikey.create",
        actor=principal_actor(principal),
        resource=f"apikey:{record.key_prefix}",
        decision="allow",
        reason=f"创建 {payload.role} 角色密钥",
        detail={"role": payload.role, "key_id": record.id},
    )
    db.commit()
    # 明文只在这个响应里出现一次，之后无法再取回。
    return ApiKeyCreated(**ApiKeyRead.model_validate(record).model_dump(), api_key=raw)


@router.get("/api-keys", response_model=Page[ApiKeyRead], responses=_ERRORS)
def list_api_keys(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(WRITE_KEYS)),
    db: Session = Depends(get_db),
):
    items, total = ApiKeyService(db).list_all(limit=limit, offset=offset)
    return Page[ApiKeyRead](items=items, total=total, limit=limit, offset=offset)


@router.patch("/api-keys/{key_id}", response_model=ApiKeyRead, responses=_ERRORS)
def update_api_key(
    key_id: str,
    payload: ApiKeyUpdate,
    principal: Principal = Depends(require_permission(WRITE_KEYS)),
    db: Session = Depends(get_db),
):
    if payload.enabled is None:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有可更新的字段（当前仅支持 enabled）",
            status_code=422,
        )

    record = ApiKeyService(db).set_enabled(key_id, payload.enabled)
    AuditService(db).record(
        action="apikey.update",
        actor=principal_actor(principal),
        resource=f"apikey:{record.key_prefix}",
        decision="allow" if record.enabled else "deny",
        reason="启用密钥" if record.enabled else "停用密钥",
        detail={"key_id": record.id, "enabled": record.enabled},
    )
    db.commit()
    return record

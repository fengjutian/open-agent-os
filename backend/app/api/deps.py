"""鉴权依赖：解析凭据 → 得到 Principal → 校验权限点。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.request_context import principal_key_id_var
from app.core.security import ANONYMOUS_ADMIN, Principal
from app.db.session import get_db
from app.services.api_keys import ApiKeyService
from app.services.rate_limit import enforce_rate_limit


def _extract_key(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


async def get_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> Principal:
    """解析请求身份，并做每密钥的请求限流。认证关闭时退化为匿名管理员（仅限本地开发）。"""
    if not settings.auth_enabled:
        principal = ANONYMOUS_ADMIN
    else:
        raw = _extract_key(authorization, x_api_key)
        if raw is None:
            raise AppError(
                ErrorCode.UNAUTHORIZED,
                "缺少 API Key：请使用 Authorization: Bearer <key> 或 X-API-Key 头",
                status_code=401,
            )

        authenticated = ApiKeyService(db).authenticate(raw)
        if authenticated is None:
            raise AppError(ErrorCode.UNAUTHORIZED, "API Key 无效或已停用", status_code=401)
        principal = authenticated

    # 认证通过后才限流：拿不到身份的请求没必要占用桶。
    enforce_rate_limit(principal.key_id)
    principal_key_id_var.set(principal.key_id)
    return principal


def require_permission(permission: str):
    """依赖工厂：要求当前身份具备某个权限点。"""

    async def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.can(permission):
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"当前角色 {principal.role} 无权执行该操作（需要 {permission}）",
                status_code=403,
            )
        return principal

    return dependency


def principal_actor(principal: Principal) -> str:
    """审计里的操作主体。"""
    return "anonymous" if principal.is_anonymous else f"key:{principal.key_id}"

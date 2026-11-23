"""鉴权依赖：解析凭据 → 得到 Principal → 校验权限点。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.oidc import looks_like_jwt, principal_from_token
from app.core.request_context import principal_key_id_var
from app.core.security import ANONYMOUS_ADMIN, Principal
from app.core.tenancy import set_tenant
from app.db.session import get_db
from app.services.api_keys import ApiKeyService
from app.services.rate_limit import enforce_rate_limit
from app.services.tenants import assert_tenant_active, resolve_membership


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
    """解析请求身份，并做每密钥的请求限流。认证关闭时退化为匿名管理员（仅限本地开发）。

    凭据带两个点就按 JWT（OIDC）处理，否则按 API Key 查库：
    我们自己的 API Key 形如 `oak_xxx`，不含点，两者不会混淆。
    """
    if not settings.auth_enabled:
        principal = ANONYMOUS_ADMIN
    else:
        raw = _extract_key(authorization, x_api_key)
        if raw is None:
            raise AppError(
                ErrorCode.UNAUTHORIZED,
                "缺少凭据：请使用 Authorization: Bearer <API Key 或 JWT> 或 X-API-Key 头",
                status_code=401,
            )

        if looks_like_jwt(raw):
            principal = await principal_from_token(raw)
        else:
            authenticated = ApiKeyService(db).authenticate(raw)
            if authenticated is None:
                raise AppError(ErrorCode.UNAUTHORIZED, "API Key 无效或已停用", status_code=401)
            principal = authenticated
    # 认证通过后才限流：拿不到身份的请求没必要占用桶。
    await enforce_rate_limit(principal.key_id)
    principal_key_id_var.set(principal.key_id)
    # 认证完成后才知道租户；之后所有查询都会被限制在该租户内。
    set_tenant(principal.tenant_id)
    # 停用的租户立即拒绝，且不区分「不存在」与「已停用」。
    assert_tenant_active(db, principal.tenant_id)
    # 成员关系可以覆盖凭据里的角色（平台按租户授权）。
    principal = resolve_membership(db, principal)
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

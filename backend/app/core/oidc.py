"""OIDC / JWT 认证。

设计要点：

- 与 API Key **并存**：凭据里带两个点就按 JWT 处理（我们自己的 API Key 用
  `oak_` 前缀 + urlsafe 字符，不含点），否则走 API Key 路径。
- 验签只信任 IdP 的 JWKS，本地不保存任何公钥；JWKS 有 TTL 缓存。
- 遇到未知 `kid` 会强制刷新一次缓存再判定失败——这样 IdP 轮换密钥不需要重启服务。
- 角色来自 claim，且只接受已定义的角色名；未知角色**降级为默认角色**而不是报错，
  避免 IdP 加了个新角色就把所有人挡在门外。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.security import ROLES, Principal, principal_for
from app.core.tenancy import DEFAULT_TENANT

logger = logging.getLogger(__name__)

_JWKS_TIMEOUT = 10.0

# url -> (expires_at_monotonic, jwks)
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}
# 测试注入点：替换 JWKS 拉取的 transport
_jwks_transport: httpx.AsyncBaseTransport | None = None


def set_jwks_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试或私有部署时替换 JWKS 请求的 transport。"""
    global _jwks_transport
    _jwks_transport = transport
    _jwks_cache.clear()


def reset_jwks_cache() -> None:
    _jwks_cache.clear()


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2


async def _fetch_jwks(*, force: bool = False) -> dict[str, Any]:
    url = settings.oidc_jwks_url
    now = time.monotonic()

    if not force:
        cached = _jwks_cache.get(url)
        if cached and cached[0] > now:
            return cached[1]

    try:
        async with httpx.AsyncClient(timeout=_JWKS_TIMEOUT, transport=_jwks_transport) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("jwks fetch failed", extra={"fields": {"url": url, "error": type(exc).__name__}})
        raise AppError(ErrorCode.UNAUTHORIZED, "无法获取 OIDC 公钥（JWKS）", status_code=401) from exc

    if response.status_code >= 400:
        raise AppError(ErrorCode.UNAUTHORIZED, "无法获取 OIDC 公钥（JWKS）", status_code=401)

    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("keys"):
        raise AppError(ErrorCode.UNAUTHORIZED, "JWKS 内容非法", status_code=401)

    _jwks_cache[url] = (now + settings.oidc_jwks_cache_seconds, payload)
    return payload


def _select_key(jwks: dict[str, Any], *, kid: str | None, algorithm: str):
    candidates = jwks.get("keys") or []
    for entry in candidates:
        if kid is not None and entry.get("kid") != kid:
            continue
        try:
            return jwt.PyJWK(entry, algorithm=algorithm).key
        except (jwt.PyJWTError, ValueError, KeyError):
            continue
    return None


async def principal_from_token(token: str) -> Principal:
    if not settings.oidc_enabled:
        raise AppError(
            ErrorCode.UNAUTHORIZED,
            "服务端未启用 OIDC：请改用 API Key，或配置 OIDC_ISSUER / OIDC_AUDIENCE / OIDC_JWKS_URL",
            status_code=401,
        )

    algorithms = settings.oidc_algorithm_list
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "JWT 格式非法", status_code=401) from exc

    algorithm = str(header.get("alg") or "")
    if algorithm not in algorithms:
        raise AppError(
            ErrorCode.UNAUTHORIZED, f"不支持的签名算法：{algorithm or '(none)'}", status_code=401
        )

    kid = header.get("kid")
    key = _select_key(await _fetch_jwks(), kid=kid, algorithm=algorithm)
    if key is None:
        # 可能是 IdP 刚轮换了密钥：刷新一次再试
        key = _select_key(await _fetch_jwks(force=True), kid=kid, algorithm=algorithm)
    if key is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "找不到匹配的签名密钥（kid）", status_code=401)

    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=algorithms,
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "token 已过期", status_code=401) from exc
    except jwt.InvalidAudienceError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "token 受众不匹配", status_code=401) from exc
    except jwt.InvalidIssuerError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "token 签发者不匹配", status_code=401) from exc
    except jwt.MissingRequiredClaimError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "token 缺少必需字段", status_code=401) from exc
    except jwt.PyJWTError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, f"token 校验失败：{exc}", status_code=401) from exc

    return _principal_from_claims(claims)


def _principal_from_claims(claims: dict[str, Any]) -> Principal:
    subject = str(claims.get("sub") or "")
    raw_role = claims.get(settings.oidc_role_claim)
    role = str(raw_role) if raw_role else settings.oidc_default_role

    if role not in ROLES:
        logger.warning(
            "unknown role claim, falling back to default",
            extra={"fields": {"claimed_role": role, "default": settings.oidc_default_role}},
        )
        role = settings.oidc_default_role if settings.oidc_default_role in ROLES else "viewer"

    name = str(
        claims.get("preferred_username") or claims.get("email") or claims.get("name") or subject
    )
    tenant = str(claims.get(settings.oidc_tenant_claim) or DEFAULT_TENANT)
    return principal_for(
        key_id=f"oidc:{subject}",
        name=name,
        role=role,
        auth_method="oidc",
        tenant_id=tenant,
    )

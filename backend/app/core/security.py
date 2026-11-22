"""认证与最小 RBAC 的静态定义。

设计取舍：

- 用 **API Key** 而不是 OIDC：自托管场景下不需要外部 IdP 就能跑起来，
  后续接入 OIDC 时只需替换「凭据 → Principal」这一段，权限矩阵可以复用。
- 只保存密钥的 SHA-256 摘要。密钥本身是高熵随机串（非用户口令），
  因此不需要 bcrypt/argon2 这类慢哈希。
- 角色 → 权限是写死的矩阵，不允许通过 API 改；避免「改权限」本身成为攻击面。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field

KEY_PREFIX = "oak_"

# 权限点
READ = "read"
WRITE_AGENTS = "write:agents"
INVOKE_TOOLS = "invoke:tools"
WRITE_TOOLS = "write:tools"
WRITE_POLICY = "write:policy"
WRITE_KEYS = "write:keys"
WRITE_SKILLS = "write:skills"

ALL_PERMISSIONS: frozenset[str] = frozenset(
    {READ, WRITE_AGENTS, INVOKE_TOOLS, WRITE_TOOLS, WRITE_POLICY, WRITE_KEYS, WRITE_SKILLS}
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    # 只读：可以看 Agent、会话、消息、工具、审计
    "viewer": frozenset({READ}),
    # 开发者：可以建 Agent、发消息、调用已被放行的工具，但不能改工具与策略
    "developer": frozenset({READ, WRITE_AGENTS, INVOKE_TOOLS}),
    # 管理员：全部
    "admin": ALL_PERMISSIONS,
}

ROLES: frozenset[str] = frozenset(ROLE_PERMISSIONS)


@dataclass(frozen=True)
class Principal:
    """一次请求的身份。"""

    key_id: str
    name: str
    role: str
    permissions: frozenset[str] = field(default_factory=frozenset)

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    @property
    def is_anonymous(self) -> bool:
        return self.key_id == "anonymous"


# 仅在 AUTH_ENABLED=false（本地开发）时使用：等价于匿名管理员。
ANONYMOUS_ADMIN = Principal(
    key_id="anonymous",
    name="anonymous",
    role="admin",
    permissions=ALL_PERMISSIONS,
)


def principal_for(*, key_id: str, name: str, role: str) -> Principal:
    return Principal(
        key_id=key_id,
        name=name,
        role=role,
        permissions=ROLE_PERMISSIONS.get(role, frozenset()),
    )


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def key_prefix(raw: str) -> str:
    """用于列表展示的前缀，不参与鉴权。"""
    return raw[:12]

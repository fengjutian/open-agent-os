"""认证与 RBAC。"""

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_principal
from app.core.config import settings
from app.core.security import hash_api_key
from app.db.session import SessionLocal
from app.main import app
from app.models.entities import ApiKey


@pytest.fixture
def as_role(client):
    """把共用 client 临时切换成其他身份（或匿名）。"""
    original = client.headers.get("Authorization")

    def _switch(raw: str | None) -> None:
        if raw is None:
            client.headers.pop("authorization", None)
        else:
            client.headers["Authorization"] = f"Bearer {raw}"

    yield _switch
    if original is not None:
        client.headers["Authorization"] = original


# ---------------------------------------------------------------------------
# 凭据校验
# ---------------------------------------------------------------------------


def test_health_is_public():
    with TestClient(app) as anon:
        assert anon.get("/health").status_code == 200


def test_missing_credentials_are_rejected(as_role):
    as_role(None)
    with TestClient(app) as anon:
        response = anon.get("/api/v1/agents")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_invalid_key_is_rejected(as_role):
    as_role("oak_not-a-real-key")
    with TestClient(app) as anon:
        response = anon.get("/api/v1/agents")

    assert response.status_code == 401


def test_disabled_key_is_rejected(client, make_key, as_role):
    raw = make_key("viewer")
    with SessionLocal() as db:
        record = db.query(ApiKey).filter(ApiKey.key_hash == hash_api_key(raw)).one()
        record.enabled = False
        db.commit()

    as_role(raw)
    assert client.get("/api/v1/agents").status_code == 401


def test_x_api_key_header_works(client, make_key, as_role):
    raw = make_key("admin")
    as_role(None)
    response = client.get("/api/v1/agents", headers={"X-API-Key": raw})
    assert response.status_code == 200


def test_authentication_updates_last_used(client, make_key, as_role):
    raw = make_key("viewer")
    as_role(raw)
    client.get("/api/v1/agents")

    with SessionLocal() as db:
        record = db.query(ApiKey).filter(ApiKey.key_hash == hash_api_key(raw)).one()
        assert record.last_used_at is not None


# ---------------------------------------------------------------------------
# RBAC 矩阵
# ---------------------------------------------------------------------------


def test_viewer_can_read(client, make_key, as_role):
    as_role(make_key("viewer"))
    assert client.get("/api/v1/agents").status_code == 200


def test_viewer_cannot_write_agents(client, make_key, as_role):
    as_role(make_key("viewer"))
    response = client.post("/api/v1/agents", json={"name": "nope"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_viewer_cannot_manage_tools_or_policy(client, make_key, as_role):
    as_role(make_key("viewer"))

    assert client.post("/api/v1/tools", json={"name": "echo"}).status_code == 403
    assert (
        client.post("/api/v1/policy-rules", json={"tool_name": "echo", "effect": "allow"}).status_code
        == 403
    )
    assert client.post("/api/v1/api-keys", json={"name": "x"}).status_code == 403


def test_developer_can_write_agents(client, make_key, as_role):
    as_role(make_key("developer"))
    assert client.post("/api/v1/agents", json={"name": "dev-agent"}).status_code == 201


def test_developer_cannot_register_tools_or_policy(client, make_key, as_role):
    as_role(make_key("developer"))

    assert client.post("/api/v1/tools", json={"name": "echo"}).status_code == 403
    assert (
        client.post("/api/v1/policy-rules", json={"tool_name": "echo", "effect": "allow"}).status_code
        == 403
    )


def test_developer_can_invoke_allowed_tool(client, agent, allow_echo, make_key, as_role):
    """开发者可以用别人已经放行的工具，但自己不能去放行。"""
    as_role(make_key("developer"))

    response = client.post(
        f"/api/v1/agents/{agent['id']}/tools/echo/invoke",
        json={"arguments": {"text": "hi"}},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


def test_admin_can_manage_everything(client):
    assert client.post("/api/v1/tools", json={"name": "echo"}).status_code == 201
    assert (
        client.post("/api/v1/policy-rules", json={"tool_name": "echo", "effect": "allow"}).status_code
        == 201
    )
    assert client.post("/api/v1/api-keys", json={"name": "another"}).status_code == 201


# ---------------------------------------------------------------------------
# 密钥管理
# ---------------------------------------------------------------------------


def test_create_key_returns_plaintext_once(client):
    response = client.post("/api/v1/api-keys", json={"name": "ci-key", "role": "developer"})
    assert response.status_code == 201

    body = response.json()
    assert body["api_key"].startswith("oak_")
    assert body["key_prefix"] == body["api_key"][:12]
    assert body["role"] == "developer"

    # 列表里不能再出现明文，也不能出现哈希
    listed = client.get("/api/v1/api-keys").json()
    serialized = str(listed)
    assert body["api_key"] not in serialized
    assert hash_api_key(body["api_key"]) not in serialized


def test_created_key_actually_works(client, as_role):
    raw = client.post("/api/v1/api-keys", json={"name": "fresh", "role": "viewer"}).json()["api_key"]
    as_role(raw)
    assert client.get("/api/v1/agents").status_code == 200


def test_duplicate_key_name_conflicts(client):
    client.post("/api/v1/api-keys", json={"name": "dup", "role": "viewer"})
    response = client.post("/api/v1/api-keys", json={"name": "dup", "role": "viewer"})

    assert response.status_code == 409


def test_cannot_disable_last_admin_key(client):
    """停用最后一个 admin 密钥会把自己锁在门外，必须拦住。"""
    with SessionLocal() as db:
        admin = db.query(ApiKey).filter(ApiKey.role == "admin").one()
        admin_id = admin.id

    response = client.patch(f"/api/v1/api-keys/{admin_id}", json={"enabled": False})

    assert response.status_code == 409


def test_can_disable_non_admin_key(client):
    created = client.post("/api/v1/api-keys", json={"name": "temp", "role": "viewer"}).json()
    response = client.patch(f"/api/v1/api-keys/{created['id']}", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False


# ---------------------------------------------------------------------------
# 兼容开关与审计
# ---------------------------------------------------------------------------


def test_auth_can_be_disabled_for_local_dev(client, monkeypatch):
    """AUTH_ENABLED=false 等价于匿名管理员——这是本地开发的逃生口。"""
    monkeypatch.setattr(settings, "auth_enabled", False)

    with TestClient(app) as anon:
        response = anon.post("/api/v1/tools", json={"name": "noauth_tool"})

    assert response.status_code == 201


def test_audit_records_the_principal(client):
    client.post("/api/v1/tools", json={"name": "audited_tool"})

    logs = client.get("/api/v1/audit-logs", params={"action": "tool.register"}).json()
    actor = logs["items"][0]["actor"]
    assert actor.startswith("key:")


# ---------------------------------------------------------------------------
# 结构性防呆：新增路由不许漏掉鉴权
# ---------------------------------------------------------------------------


def _route_requires_auth(route) -> bool:
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        if dependant.call is get_principal:
            return True
        stack.extend(dependant.dependencies)
    return False


def test_every_api_route_requires_authentication():
    from fastapi.routing import APIRoute

    unprotected = [
        (route.path, sorted(route.methods))
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/api/v1")
        and not _route_requires_auth(route)
    ]

    assert unprotected == [], f"以下路由没有鉴权依赖: {unprotected}"


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/api/v1/agents", {}),
        ("post", "/api/v1/agents", {"json": {"name": "x"}}),
        ("get", "/api/v1/tools", {}),
        ("post", "/api/v1/tools", {"json": {"name": "echo"}}),
        ("post", "/api/v1/policy-rules", {"json": {"tool_name": "echo", "effect": "allow"}}),
        ("get", "/api/v1/audit-logs", {}),
        ("get", "/api/v1/tool-invocations", {}),
        ("get", "/api/v1/api-keys", {}),
        ("post", "/api/v1/api-keys", {"json": {"name": "x"}}),
    ],
)
def test_representative_endpoints_reject_anonymous(method, path, kwargs):
    with TestClient(app) as anon:
        response = getattr(anon, method)(path, **kwargs)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"

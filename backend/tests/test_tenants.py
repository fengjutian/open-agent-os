"""组织（租户）与成员关系。"""

import pytest

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.security import principal_for
from app.db.session import SessionLocal
from app.models.entities import Tenant
from app.services.tenants import create_membership, ensure_tenant_exists, resolve_membership


def _h_raw(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


# ---------------------------------------------------------------------------
# 租户
# ---------------------------------------------------------------------------


def test_create_tenant(client):
    response = client.post("/api/v1/tenants", json={"id": "acme", "name": "Acme Inc"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == "acme"
    assert body["status"] == "active"


def test_duplicate_tenant_conflicts(client):
    assert client.post("/api/v1/tenants", json={"id": "acme", "name": "A"}).status_code == 201
    assert client.post("/api/v1/tenants", json={"id": "acme", "name": "A"}).status_code == 409


@pytest.mark.parametrize("tenant_id", ["A", "1abc", "x", "has space", "中文"])
def test_invalid_tenant_id_is_rejected(client, tenant_id):
    response = client.post("/api/v1/tenants", json={"id": tenant_id, "name": "x"})
    assert response.status_code == 422


def test_current_tenant_endpoint(client):
    body = client.get("/api/v1/tenants/current").json()
    assert body["id"] == "default"


def test_tenant_creation_requires_admin(client, make_key, tenant_key):
    viewer = make_key("viewer")
    response = client.post(
        "/api/v1/tenants", json={"id": "acme", "name": "A"}, headers=_h_raw(viewer)
    )
    assert response.status_code == 403


def test_tenant_creation_is_audited(client):
    client.post("/api/v1/tenants", json={"id": "acme", "name": "A"})
    logs = client.get("/api/v1/audit-logs", params={"action": "tenant.create"}).json()
    assert logs["total"] == 1
    assert logs["items"][0]["resource"] == "tenant:acme"


def test_key_for_unknown_tenant_is_rejected(client):
    response = client.post(
        "/api/v1/api-keys",
        json={"name": "ghost-key", "role": "admin", "tenant_id": "ghost"},
    )
    assert response.status_code == 404
    assert "不存在" in response.json()["error"]["message"]


def test_disabled_tenant_is_rejected(client, tenant_key):
    raw = tenant_key("acme")

    with SessionLocal() as db:
        tenant = db.get(Tenant, "acme")
        tenant.status = "disabled"
        db.commit()

    response = client.get("/api/v1/agents", headers=_h_raw(raw))
    assert response.status_code == 403
    assert "停用" in response.json()["error"]["message"]


# ---------------------------------------------------------------------------
# 成员
# ---------------------------------------------------------------------------


def test_add_and_list_membership(client):
    created = client.post(
        "/api/v1/memberships", json={"subject": "oidc:alice", "role": "developer"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["role"] == "developer"

    page = client.get("/api/v1/memberships").json()
    assert page["total"] == 1
    assert page["items"][0]["subject"] == "oidc:alice"


def test_duplicate_membership_conflicts(client):
    client.post("/api/v1/memberships", json={"subject": "oidc:alice", "role": "viewer"})
    response = client.post("/api/v1/memberships", json={"subject": "oidc:alice", "role": "viewer"})
    assert response.status_code == 409


def test_update_membership_role(client):
    created = client.post(
        "/api/v1/memberships", json={"subject": "oidc:alice", "role": "viewer"}
    ).json()

    response = client.patch(f"/api/v1/memberships/{created['id']}", json={"role": "admin"})
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_delete_membership(client):
    created = client.post(
        "/api/v1/memberships", json={"subject": "oidc:alice", "role": "viewer"}
    ).json()

    assert client.delete(f"/api/v1/memberships/{created['id']}").status_code == 204
    assert client.get("/api/v1/memberships").json()["total"] == 0


def test_membership_is_audited(client):
    client.post("/api/v1/memberships", json={"subject": "oidc:alice", "role": "viewer"})
    logs = client.get("/api/v1/audit-logs", params={"action": "membership.create"}).json()
    assert logs["total"] == 1


def test_memberships_are_isolated_between_tenants(client, tenant_key):
    # 默认租户加一个成员
    client.post("/api/v1/memberships", json={"subject": "oidc:alice", "role": "viewer"})

    key_b = tenant_key("tenant-b")
    assert client.get("/api/v1/memberships", headers=_h_raw(key_b)).json()["total"] == 0


# ---------------------------------------------------------------------------
# 角色覆盖
# ---------------------------------------------------------------------------


def _principal(**overrides):
    base = {
        "key_id": "oidc:alice",
        "name": "alice",
        "role": "admin",
        "auth_method": "oidc",
        "tenant_id": "default",
    }
    base.update(overrides)
    return principal_for(**base)


def test_membership_overrides_claimed_role(client):
    with SessionLocal() as db:
        create_membership(db, subject="oidc:alice", role="viewer")
        db.commit()
        resolved = resolve_membership(db, _principal(role="admin"))

    # IdP 说 admin，但平台把它降成 viewer
    assert resolved.role == "viewer"
    assert resolved.can("read")
    assert not resolved.can("write:agents")


def test_membership_can_promote(client):
    with SessionLocal() as db:
        create_membership(db, subject="oidc:alice", role="admin")
        db.commit()
        resolved = resolve_membership(db, _principal(role="viewer"))

    assert resolved.role == "admin"


def test_without_membership_role_falls_back_to_claim(client):
    with SessionLocal() as db:
        resolved = resolve_membership(db, _principal(role="developer"))

    assert resolved.role == "developer"


def test_require_membership_rejects_unknown_oidc_user(client, monkeypatch):
    monkeypatch.setattr(settings, "oidc_require_membership", True)

    with SessionLocal() as db, pytest.raises(AppError) as excinfo:
        resolve_membership(db, _principal(role="admin"))

    assert excinfo.value.code == ErrorCode.FORBIDDEN


def test_require_membership_does_not_affect_api_keys(client, monkeypatch):
    monkeypatch.setattr(settings, "oidc_require_membership", True)

    with SessionLocal() as db:
        resolved = resolve_membership(
            db, _principal(key_id="key-123", role="admin", auth_method="api_key")
        )

    assert resolved.role == "admin"


def test_membership_of_other_tenant_does_not_apply(client):
    from app.core.tenancy import reset_tenant, set_tenant

    with SessionLocal() as db:
        ensure_tenant_exists(db, "other", name="Other")
        token = set_tenant("other")
        try:
            create_membership(db, subject="oidc:alice", role="admin")
            db.commit()
        finally:
            reset_tenant(token)

    with SessionLocal() as db:
        resolved = resolve_membership(db, _principal(role="viewer"))

    # 别的租户里的 membership 不应影响默认租户
    assert resolved.role == "viewer"

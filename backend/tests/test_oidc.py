"""OIDC / JWT 认证。

用本地生成的 RSA 密钥对 + MockTransport 充当 IdP，不需要真实 OIDC 服务。
"""

import json
import time

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.core import oidc
from app.core.config import settings
from app.main import app

ISSUER = "https://idp.example.com"
AUDIENCE = "agent-platform"
JWKS_URL = "https://idp.example.com/jwks.json"
KID = "key-1"


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def private_pem(rsa_key) -> bytes:
    return rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.fixture(scope="session")
def other_private_pem(other_rsa_key) -> bytes:
    return other_rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _jwk(key, kid: str = KID) -> dict:
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return jwk


def _token(
    pem: bytes,
    *,
    kid: str = KID,
    role: str | None = "admin",
    subject: str = "user-1",
    expires_in: int = 300,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
) -> str:
    now = int(time.time())
    claims = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in,
        "preferred_username": "tester",
    }
    if role is not None:
        claims["role"] = role
    return pyjwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def oidc_env(monkeypatch, rsa_key):
    """配置 OIDC 并把 JWKS 请求接到本地密钥上，返回可变的密钥状态与调用计数。"""
    state = {"keys": [_jwk(rsa_key)], "fetches": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["fetches"] += 1
        assert str(request.url) == JWKS_URL
        return httpx.Response(200, json={"keys": state["keys"]})

    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_audience", AUDIENCE)
    monkeypatch.setattr(settings, "oidc_jwks_url", JWKS_URL)
    monkeypatch.setattr(settings, "oidc_role_claim", "role")
    monkeypatch.setattr(settings, "oidc_default_role", "viewer")

    oidc.set_jwks_transport(httpx.MockTransport(handler))
    oidc.reset_jwks_cache()
    yield state
    oidc.set_jwks_transport(None)


def _client(token: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_admin_token_grants_admin(oidc_env, private_pem):
    with _client(_token(private_pem, role="admin")) as client:
        assert client.get("/api/v1/agents").status_code == 200
        me = client.get("/api/v1/me").json()

    assert me["role"] == "admin"
    assert me["auth_method"] == "oidc"
    assert me["key_id"].startswith("oidc:user-1")


def test_viewer_token_cannot_write(oidc_env, private_pem):
    with _client(_token(private_pem, role="viewer")) as client:
        assert client.get("/api/v1/agents").status_code == 200
        response = client.post("/api/v1/agents", json={"name": "nope"})

    assert response.status_code == 403


def test_developer_token_can_write_agents(oidc_env, private_pem):
    with _client(_token(private_pem, role="developer")) as client:
        assert client.post("/api/v1/agents", json={"name": "dev"}).status_code == 201


def test_missing_role_falls_back_to_default(oidc_env, private_pem):
    with _client(_token(private_pem, role=None)) as client:
        me = client.get("/api/v1/me").json()
        assert me["role"] == "viewer"
        assert me["permissions"] == ["read"]


def test_unknown_role_falls_back_to_default(oidc_env, private_pem):
    with _client(_token(private_pem, role="superuser")) as client:
        me = client.get("/api/v1/me").json()

    assert me["role"] == "viewer"


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


def test_expired_token_is_rejected(oidc_env, private_pem):
    with _client(_token(private_pem, expires_in=-10)) as client:
        response = client.get("/api/v1/agents")

    assert response.status_code == 401
    assert "过期" in response.json()["error"]["message"]


def test_wrong_audience_is_rejected(oidc_env, private_pem):
    with _client(_token(private_pem, audience="someone-else")) as client:
        response = client.get("/api/v1/agents")

    assert response.status_code == 401


def test_wrong_issuer_is_rejected(oidc_env, private_pem):
    with _client(_token(private_pem, issuer="https://evil.example.com")) as client:
        response = client.get("/api/v1/agents")

    assert response.status_code == 401


def test_wrong_signature_is_rejected(oidc_env, other_private_pem):
    """同样 kid、不同私钥签出来的 token 必须被拒。"""
    with _client(_token(other_private_pem)) as client:
        response = client.get("/api/v1/agents")

    assert response.status_code == 401


def test_unknown_kid_is_rejected_after_refresh(oidc_env, private_pem):
    with _client(_token(private_pem, kid="unknown-kid")) as client:
        response = client.get("/api/v1/agents")

    assert response.status_code == 401
    # 未知 kid 会强制刷新一次缓存
    assert oidc_env["fetches"] >= 2


def test_malformed_token_is_rejected(oidc_env):
    with _client("not.a.jwt") as client:
        response = client.get("/api/v1/agents")

    assert response.status_code == 401


def test_jwt_rejected_when_oidc_not_configured(client, private_pem):
    """服务端没配 OIDC 时，JWT 不能被当成 API Key 放行。"""
    with _client(_token(private_pem)) as oidc_client:
        response = oidc_client.get("/api/v1/agents")

    assert response.status_code == 401
    assert "未启用 OIDC" in response.json()["error"]["message"]


# ---------------------------------------------------------------------------
# JWKS 缓存与密钥轮换
# ---------------------------------------------------------------------------


def test_jwks_is_cached(oidc_env, private_pem):
    token = _token(private_pem)
    with _client(token) as client:
        for _ in range(3):
            assert client.get("/api/v1/agents").status_code == 200

    assert oidc_env["fetches"] == 1


def test_key_rotation_is_picked_up(oidc_env, rsa_key, private_pem):
    # 先用旧密钥打通，填充缓存
    with _client(_token(private_pem)) as client:
        assert client.get("/api/v1/agents").status_code == 200
    assert oidc_env["fetches"] == 1

    # IdP 换了一把新密钥，旧密钥从 JWKS 移除
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    oidc_env["keys"] = [_jwk(new_key, "key-2")]
    new_pem = new_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    with _client(_token(new_pem, kid="key-2")) as client:
        assert client.get("/api/v1/agents").status_code == 200

    # 首次拉取 1 次；新 kid 在缓存里找不到 → 强制刷新 1 次，命中后即可复用
    assert oidc_env["fetches"] == 2


# ---------------------------------------------------------------------------
# 与 API Key 并存
# ---------------------------------------------------------------------------


def test_api_key_still_works_when_oidc_enabled(client, oidc_env):
    assert client.get("/api/v1/agents").status_code == 200
    me = client.get("/api/v1/me").json()
    assert me["auth_method"] == "api_key"


def test_membership_overrides_oidc_claimed_role(oidc_env, private_pem):
    """IdP 说 admin，但平台按成员关系把它降成 viewer。"""
    from app.db.session import SessionLocal
    from app.services.tenants import create_membership

    with SessionLocal() as db:
        create_membership(db, subject="oidc:user-1", role="viewer")
        db.commit()

    with _client(_token(private_pem, role="admin")) as client:
        me = client.get("/api/v1/me").json()
        denied = client.post("/api/v1/agents", json={"name": "nope"})

    assert me["role"] == "viewer"
    assert denied.status_code == 403

"""审计哈希链：改写、删除、插入都必须被发现。"""

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal, engine
from app.models.entities import AuditLog


def _verify(client) -> dict:
    return client.get("/api/v1/audit-logs/verify").json()


def _make_audits(client, count: int = 3) -> None:
    for index in range(count):
        response = client.post("/api/v1/tools", json={"name": f"tool-{index}"})
        assert response.status_code == 201, response.text


def _audit_ids() -> list[str]:
    with SessionLocal() as db:
        rows = db.execute(
            text("SELECT id FROM audit_logs ORDER BY seq")
        ).fetchall()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# 正常链路
# ---------------------------------------------------------------------------


def test_chain_is_valid_after_writes(client):
    _make_audits(client, 3)

    report = _verify(client)
    assert report["valid"] is True
    assert report["checked"] == 3
    assert report["legacy"] == 0
    assert report["broken_at"] is None


def test_empty_chain_is_valid(client):
    report = _verify(client)
    assert report["valid"] is True
    assert report["checked"] == 0


def test_chain_is_per_tenant(client, tenant_key):
    _make_audits(client, 2)

    other = tenant_key("tenant-b")
    report = client.get(
        "/api/v1/audit-logs/verify", headers={"Authorization": f"Bearer {other}"}
    ).json()

    # 别的租户看不到当前租户的审计，也就验证不到任何行
    assert report["valid"] is True
    assert report["checked"] == 0


# ---------------------------------------------------------------------------
# 篡改检测
# ---------------------------------------------------------------------------


def test_modified_entry_is_detected(client):
    _make_audits(client, 3)
    ids = _audit_ids()

    # 绕过 ORM 直接改内容（模拟有人动了数据库）
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE audit_logs SET reason = 'tampered' WHERE id = :id"),
            {"id": ids[1]},
        )

    report = _verify(client)
    assert report["valid"] is False
    assert report["broken_at"] == 2
    assert "内容被修改" in report["reason"]


def test_deleted_entry_is_detected(client):
    _make_audits(client, 3)
    ids = _audit_ids()

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM audit_logs WHERE id = :id"), {"id": ids[1]})

    report = _verify(client)
    assert report["valid"] is False
    assert "序号不连续" in report["reason"]


def test_inserted_entry_is_detected(client):
    _make_audits(client, 2)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_logs "
                "(id, tenant_id, seq, prev_hash, entry_hash, actor, action, resource, "
                " created_at) "
                "VALUES ('forged', 'default', 99, NULL, 'deadbeef', 'attacker', "
                "'tool.register', 'tool:forged', CURRENT_TIMESTAMP)"
            )
        )

    report = _verify(client)
    assert report["valid"] is False
    assert "序号不连续" in report["reason"]


def test_deleted_tail_is_detected(client):
    _make_audits(client, 3)
    ids = _audit_ids()

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM audit_logs WHERE id = :id"), {"id": ids[-1]})

    # 删除末尾不会让序号断档，链本身仍然自洽——这一点必须如实说明
    report = _verify(client)
    assert report["checked"] == 2


# ---------------------------------------------------------------------------
# ORM 层只追加保护
# ---------------------------------------------------------------------------


def test_orm_update_is_blocked(client):
    _make_audits(client, 1)
    audit_id = _audit_ids()[0]

    with SessionLocal() as db:
        row = db.get(AuditLog, audit_id)
        row.reason = "tampered"
        with pytest.raises(RuntimeError, match="只追加"):
            db.commit()
        db.rollback()


def test_orm_delete_is_blocked(client):
    _make_audits(client, 1)
    audit_id = _audit_ids()[0]

    with SessionLocal() as db:
        row = db.get(AuditLog, audit_id)
        db.delete(row)
        with pytest.raises(RuntimeError, match="只追加"):
            db.commit()
        db.rollback()


# ---------------------------------------------------------------------------
# 历史行兼容
# ---------------------------------------------------------------------------


def test_legacy_rows_are_counted_separately(client):
    _make_audits(client, 2)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_logs "
                "(id, tenant_id, seq, prev_hash, entry_hash, actor, action, resource, "
                " created_at) "
                "VALUES ('legacy-1', 'default', NULL, NULL, NULL, 'old', 'tool.register', "
                "'tool:legacy', '2020-01-01 00:00:00')"
            )
        )

    report = _verify(client)
    assert report["valid"] is True
    assert report["checked"] == 2
    assert report["legacy"] == 1

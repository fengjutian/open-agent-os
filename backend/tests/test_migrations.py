"""迁移与模型一致性。

`alembic_version` 是 Alembic 自己的记账表，不参与比较。
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from app.db.session import Base, engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "LOG_LEVEL": "WARNING",
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


def test_migrations_are_up_to_date_and_match_models(client):
    """迁移执行后的库结构必须与 ORM 模型完全一致（防止迁移与模型漂移）。"""
    from app.models import entities  # noqa: F401

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)

    relevant = [item for item in diff if "alembic_version" not in str(item)]
    assert relevant == [], f"迁移与模型不一致: {relevant}"


def test_expected_tables_exist(client):
    from sqlalchemy import inspect

    tables = set(inspect(engine).get_table_names())
    assert {"agents", "conversations", "messages"} <= tables


def test_upgrade_with_existing_rows_adds_skill_names(tmp_path):
    """回归：新增 NOT NULL 列必须能在已有数据的库上完成升级。"""
    db_path = tmp_path / "existing.db"

    # 先升到引入 skill_names 之前的那一版
    result = _alembic(db_path, "upgrade", "f923738b6a21")
    assert result.returncode == 0, result.stderr[-2000:]

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agents "
            "(id, name, description, system_prompt, model, created_at, updated_at) "
            "VALUES ('a1', 'old agent', NULL, 'you are helpful', 'm', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        connection.commit()

    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr[-2000:]

    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT skill_names FROM agents WHERE id = 'a1'").fetchone()

    assert row is not None
    assert row[0] == "[]"


def test_tenant_migration_backfills_existing_rows(tmp_path):
    """回归：加 tenant_id 时已有数据必须被回填成默认租户，而不是升级失败或变 NULL。"""
    db_path = tmp_path / "pre_tenant.db"

    # 先升到引入租户隔离之前的那一版
    result = _alembic(db_path, "upgrade", "92fff69ba6f8")
    assert result.returncode == 0, result.stderr[-2000:]

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agents "
            "(id, name, description, system_prompt, model, skill_names, created_at, updated_at) "
            "VALUES ('a1', 'legacy', NULL, 'p', 'm', '[]', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        connection.execute(
            "INSERT INTO tool_definitions "
            "(id, name, description, input_schema, permissions, timeout_seconds, "
            " execution_type, config, enabled, created_at, updated_at) "
            "VALUES ('t1', 'legacy_tool', '', '{}', '[]', 10, 'builtin', NULL, 1, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        connection.commit()

    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr[-2000:]

    with sqlite3.connect(db_path) as connection:
        agent_tenant = connection.execute(
            "SELECT tenant_id FROM agents WHERE id = 'a1'"
        ).fetchone()
        tool_tenant = connection.execute(
            "SELECT tenant_id FROM tool_definitions WHERE id = 't1'"
        ).fetchone()

    assert agent_tenant == ("default",)
    assert tool_tenant == ("default",)

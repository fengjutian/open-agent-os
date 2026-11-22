"""PostgreSQL 方言校验：迁移必须能为 PG 渲染出合法 SQL。

本机没有 PG 服务时，用 Alembic 的离线模式（--sql）渲染 SQL 来验证方言兼容性：
不连接数据库，但会走真实的迁移脚本与 PG 方言编译器。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("psycopg", reason="需要 psycopg 才能渲染 PostgreSQL 方言")

BACKEND_ROOT = Path(__file__).resolve().parents[1]

TABLES = [
    "agents",
    "conversations",
    "messages",
    "tool_definitions",
    "policy_rules",
    "tool_invocations",
    "audit_logs",
    "api_keys",
    "runs",
    "run_events",
]


def test_migrations_render_for_postgres():
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql+psycopg://user:password@localhost:5432/agent_platform",
        "LOG_LEVEL": "WARNING",
    }

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    sql = result.stdout

    for table in TABLES:
        assert f"CREATE TABLE {table}" in sql, f"{table} 未在 PostgreSQL SQL 中出现"

    # 方言差异：PG 用 VARCHAR/JSON，且不应出现 SQLite 专有的批处理重建
    assert "VARCHAR(36)" in sql
    assert "JSONB" not in sql and "JSON" in sql
    assert "batch_alter_table" not in sql


def test_postgres_url_does_not_use_sqlite_batch_mode(monkeypatch):
    """render_as_batch 必须只在 SQLite 下开启。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://u:p@localhost/db")
    assert settings.is_sqlite is False

    monkeypatch.setattr(settings, "database_url", "sqlite:///./x.db")
    assert settings.is_sqlite is True

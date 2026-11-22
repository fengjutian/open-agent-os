"""迁移与模型一致性。

`alembic_version` 是 Alembic 自己的记账表，不参与比较。
"""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from app.db.session import Base, engine


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

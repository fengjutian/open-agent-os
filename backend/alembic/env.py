"""Alembic 运行环境。

数据库 URL 统一从 app.core.config.settings 读取，避免 alembic.ini 与 .env 两处配置漂移。
"""

from __future__ import annotations

from sqlalchemy import create_engine, pool

from alembic import context
from app.core.config import settings
from app.db.session import Base
from app.models import entities  # noqa: F401  确保模型已注册到 Base.metadata

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = settings.database_url
    # configparser 会把 % 当插值符号，转义一下。
    return url.replace("%", "%%")


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite 不支持大多数 ALTER TABLE，batch 模式让它通过重建表来表达变更。
            render_as_batch=settings.is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

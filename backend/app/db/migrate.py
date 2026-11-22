"""以编程方式运行 Alembic 迁移。

应用启动时自动 upgrade 到 head，取代原来的 Base.metadata.create_all：
schema 变更从此只有迁移这一条真相。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from app.core.config import settings

# backend/app/db/migrate.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_DIR = BACKEND_ROOT / "alembic"


def build_alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    return config


def run_migrations() -> None:
    """把数据库升级到最新版本。空库会从零建表。"""
    command.upgrade(build_alembic_config(), "head")

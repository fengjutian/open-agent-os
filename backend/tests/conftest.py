"""测试环境准备。

注意：DATABASE_URL 必须在 import app.* **之前**设置——settings 是模块级单例，
引擎在 app.db.session 导入时就已经建好，之后再改配置不会生效。
"""

import os
import tempfile
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="agent-platform-tests-"))
os.environ["DATABASE_URL"] = "sqlite:///" + (_TMP_DIR / "test.db").as_posix()
os.environ["MODEL_PROVIDER"] = "mock"
os.environ["MODEL_API_KEY"] = ""
os.environ["LOG_LEVEL"] = "WARNING"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db.session import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    # with 语句会触发 lifespan：配置日志并执行 Alembic 迁移。
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_tables(client):
    """每个用例前清空业务表，保证用例之间互不影响。"""
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    yield


@pytest.fixture
def agent(client) -> dict:
    response = client.post("/api/v1/agents", json={"name": "测试 Agent"})
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def conversation(client, agent) -> dict:
    response = client.post(
        f"/api/v1/agents/{agent['id']}/conversations",
        json={"title": "测试会话"},
    )
    assert response.status_code == 201
    return response.json()

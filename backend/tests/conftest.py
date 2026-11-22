"""测试环境准备。

注意：DATABASE_URL 必须在 import app.* **之前**设置——settings 是模块级单例，
引擎在 app.db.session 导入时就已经建好，之后再改配置不会生效。
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="agent-platform-tests-"))


def _cleanup_tmp_dir() -> None:
    """退出时清理临时库。必须先关掉连接池，否则 Windows 上文件句柄未释放会删不掉。"""
    try:
        from app.db.session import engine

        engine.dispose()
    except Exception:  # 清理失败不该影响测试结果
        pass
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


# 进程退出时清理临时库，避免反复跑测试在系统临时目录堆积文件。
atexit.register(_cleanup_tmp_dir)
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


ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


@pytest.fixture
def echo_tool(client) -> dict:
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "echo",
            "description": "回显传入的文本",
            "input_schema": ECHO_SCHEMA,
            "permissions": ["read:text"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def allow_echo(client, agent, echo_tool) -> dict:
    """给 agent 显式放行 echo——默认是拒绝的，必须显式授权。"""
    response = client.post(
        "/api/v1/policy-rules",
        json={
            "agent_id": agent["id"],
            "tool_name": "echo",
            "effect": "allow",
            "priority": 100,
            "note": "测试放行",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()

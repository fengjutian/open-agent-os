"""MCP：客户端、治理集成与工具同步。"""

import asyncio
import sys
from pathlib import Path

import pytest

from app.core.config import McpServerConfig, settings
from app.services.mcp_client import McpError, McpStdioClient

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"

MCP_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


@pytest.fixture
def mcp_server(monkeypatch) -> McpServerConfig:
    config = McpServerConfig(command=sys.executable, args=[str(FIXTURE)], timeout_seconds=20)
    monkeypatch.setattr(settings, "mcp_servers", {"echo": config})
    return config


def _client(config: McpServerConfig, *, timeout: float = 20.0) -> McpStdioClient:
    return McpStdioClient(command=config.command, args=config.args, timeout=timeout)


def _register_mcp_tool(
    client, *, name: str = "mcp_echo", server: str = "echo", remote: str = "echo", **extra
):
    return client.post(
        "/api/v1/tools",
        json={
            "name": name,
            "description": "MCP 回显",
            "input_schema": MCP_TOOL_SCHEMA,
            "execution_type": "mcp",
            "config": {"server": server, "tool": remote},
            **extra,
        },
    )


def _allow(client, agent_id: str, tool_name: str):
    return client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent_id, "tool_name": tool_name, "effect": "allow", "priority": 100},
    )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def test_client_initialize_and_list_tools(mcp_server):
    async def scenario():
        async with _client(mcp_server) as session:
            return session.server_info, await session.list_tools()

    info, tools = asyncio.run(scenario())

    assert info["name"] == "echo-fixture"
    assert {"echo", "slow", "boom", "broken_schema"} <= {tool.name for tool in tools}


def test_client_call_tool(mcp_server):
    async def scenario():
        async with _client(mcp_server) as session:
            return await session.call_tool("echo", {"text": "hello"})

    result = asyncio.run(scenario())
    assert result.is_error is False
    assert result.text() == "hello"


def test_client_unknown_tool_raises(mcp_server):
    async def scenario():
        async with _client(mcp_server) as session:
            await session.call_tool("nonexistent", {})

    with pytest.raises(McpError) as excinfo:
        asyncio.run(scenario())
    assert "Unknown tool" in str(excinfo.value)


def test_client_read_timeout(mcp_server):
    async def scenario():
        async with _client(mcp_server, timeout=0.2) as session:
            await session.call_tool("slow", {"seconds": 1})

    with pytest.raises(McpError) as excinfo:
        asyncio.run(scenario())
    assert "超时" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 注册与治理
# ---------------------------------------------------------------------------


def test_register_mcp_tool_requires_declared_server(client, mcp_server):
    response = _register_mcp_tool(client, server="not-declared")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "MCP_SERVERS" in response.json()["error"]["message"]


def test_register_mcp_tool_requires_config(client, mcp_server):
    response = client.post(
        "/api/v1/tools",
        json={"name": "mcp_bad", "execution_type": "mcp"},
    )
    assert response.status_code == 422


def test_mcp_tool_is_governed_and_executes(client, agent, mcp_server):
    tool = _register_mcp_tool(client)
    assert tool.status_code == 201, tool.text
    assert tool.json()["execution_type"] == "mcp"
    assert tool.json()["config"] == {"server": "echo", "tool": "echo"}

    # 默认拒绝：没放行前不能调
    denied = client.post(
        f"/api/v1/agents/{agent['id']}/tools/mcp_echo/invoke",
        json={"arguments": {"text": "hello"}},
    ).json()
    assert denied["status"] == "denied"
    assert denied["error_code"] == "policy_denied"

    _allow(client, agent["id"], "mcp_echo")
    allowed = client.post(
        f"/api/v1/agents/{agent['id']}/tools/mcp_echo/invoke",
        json={"arguments": {"text": "hello"}},
    ).json()

    assert allowed["status"] == "succeeded"
    assert allowed["result"]["text"] == "hello"


def test_mcp_tool_still_validates_arguments(client, agent, mcp_server):
    _register_mcp_tool(client)
    _allow(client, agent["id"], "mcp_echo")

    body = client.post(
        f"/api/v1/agents/{agent['id']}/tools/mcp_echo/invoke",
        json={"arguments": {"text": 123}},
    ).json()

    assert body["status"] == "denied"
    assert body["error_code"] == "tool_arguments_invalid"


def test_mcp_server_error_surfaces_as_failure(client, agent, mcp_server):
    _register_mcp_tool(client, name="mcp_boom", remote="boom", input_schema={"type": "object"})
    _allow(client, agent["id"], "mcp_boom")

    body = client.post(
        f"/api/v1/agents/{agent['id']}/tools/mcp_boom/invoke", json={"arguments": {}}
    ).json()

    assert body["status"] == "failed"
    assert body["error_code"] == "tool_execution_failed"
    assert "boom failure" in body["error_message"]


def test_mcp_timeout_maps_to_tool_timeout(client, agent, mcp_server):
    _register_mcp_tool(
        client,
        name="mcp_slow",
        remote="slow",
        input_schema={"type": "object"},
        timeout_seconds=0.3,
    )
    _allow(client, agent["id"], "mcp_slow")

    body = client.post(
        f"/api/v1/agents/{agent['id']}/tools/mcp_slow/invoke",
        json={"arguments": {"seconds": 2}},
    ).json()

    assert body["status"] == "failed"
    assert body["error_code"] == "tool_timeout"


def test_undeclared_server_fails_at_execution(client, agent, mcp_server, monkeypatch):
    """工具已注册但服务器被撤掉：执行阶段必须显式失败，而不是悄悄成功。"""
    _register_mcp_tool(client)
    _allow(client, agent["id"], "mcp_echo")
    monkeypatch.setattr(settings, "mcp_servers", {})

    body = client.post(
        f"/api/v1/agents/{agent['id']}/tools/mcp_echo/invoke",
        json={"arguments": {"text": "hi"}},
    ).json()

    assert body["status"] == "failed"
    assert body["error_code"] == "tool_execution_failed"
    assert "未在配置中声明" in body["error_message"]


def test_mcp_invocation_is_audited(client, agent, mcp_server):
    _register_mcp_tool(client)
    _allow(client, agent["id"], "mcp_echo")
    run_id = client.post(
        f"/api/v1/agents/{agent['id']}/tools/mcp_echo/invoke",
        json={"arguments": {"text": "hi"}},
    ).json()["run_id"]

    audits = client.get("/api/v1/audit-logs", params={"run_id": run_id}).json()
    assert audits["total"] == 1
    assert audits["items"][0]["decision"] == "allow"


# ---------------------------------------------------------------------------
# 同步
# ---------------------------------------------------------------------------


def test_sync_registers_remote_tools(client, mcp_server):
    result = client.post("/api/v1/mcp/servers/echo/sync")

    assert result.status_code == 200, result.text
    body = result.json()
    assert body["server"] == "echo"

    names = {tool["name"] for tool in body["registered"]}
    assert {"echo.echo", "echo.slow", "echo.boom"} <= names
    # 非法 inputSchema 的工具应当被跳过并说明原因
    assert any("broken_schema" in reason for reason in body["skipped"])

    registered = client.get("/api/v1/tools").json()
    assert registered["total"] == len(body["registered"])


def test_sync_is_idempotent(client, mcp_server):
    first = client.post("/api/v1/mcp/servers/echo/sync").json()
    second = client.post("/api/v1/mcp/servers/echo/sync").json()

    assert {tool["id"] for tool in first["registered"]} == {
        tool["id"] for tool in second["registered"]
    }
    assert client.get("/api/v1/tools").json()["total"] == len(first["registered"])


def test_sync_unknown_server_returns_404(client, mcp_server):
    assert client.post("/api/v1/mcp/servers/nope/sync").status_code == 404


def test_synced_tool_can_be_invoked(client, agent, mcp_server):
    client.post("/api/v1/mcp/servers/echo/sync")
    _allow(client, agent["id"], "echo.echo")

    body = client.post(
        f"/api/v1/agents/{agent['id']}/tools/echo.echo/invoke",
        json={"arguments": {"text": "from sync"}},
    ).json()

    assert body["status"] == "succeeded"
    assert body["result"]["text"] == "from sync"

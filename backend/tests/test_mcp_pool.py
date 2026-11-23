"""MCP 连接池：复用、串行化、回收与失败重建。"""

import asyncio
import sys
from pathlib import Path

import pytest

from app.core.config import McpServerConfig, settings
from app.services.mcp_client import McpError
from app.services.mcp_pool import McpPool, get_mcp_pool

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


@pytest.fixture
def server_config(monkeypatch) -> McpServerConfig:
    config = McpServerConfig(command=sys.executable, args=[str(FIXTURE)], timeout_seconds=20)
    monkeypatch.setattr(settings, "mcp_servers", {"echo": config})
    return config


def run(coro):
    return asyncio.run(coro)


def test_pool_reuses_one_process(server_config):
    async def scenario():
        pool = McpPool(idle_seconds=60)
        try:
            async with pool.session("echo", server_config, timeout=10) as client:
                await client.call_tool("echo", {"text": "a"})
            async with pool.session("echo", server_config, timeout=10) as client:
                await client.call_tool("echo", {"text": "b"})
            return pool.spawn_count
        finally:
            await pool.close_all()

    assert run(scenario()) == 1


def test_idle_connection_is_recycled(server_config):
    async def scenario():
        pool = McpPool(idle_seconds=0)  # 立即视为过期
        try:
            async with pool.session("echo", server_config, timeout=10) as client:
                await client.call_tool("echo", {"text": "a"})
            async with pool.session("echo", server_config, timeout=10) as client:
                await client.call_tool("echo", {"text": "b"})
            return pool.spawn_count
        finally:
            await pool.close_all()

    assert run(scenario()) == 2


def test_concurrent_calls_are_serialized_on_one_connection(server_config):
    """并发请求不能交叉响应：池按服务器串行化，且只起一个进程。"""

    async def scenario():
        pool = McpPool(idle_seconds=60)

        async def one(text: str) -> str:
            async with pool.session("echo", server_config, timeout=10) as client:
                result = await client.call_tool("echo", {"text": text})
                return result.text()

        try:
            results = await asyncio.gather(one("1"), one("2"), one("3"), one("4"))
            return results, pool.spawn_count
        finally:
            await pool.close_all()

    results, spawns = run(scenario())
    assert sorted(results) == ["1", "2", "3", "4"]
    assert spawns == 1


def test_dead_process_is_replaced(server_config):
    async def scenario():
        pool = McpPool(idle_seconds=60)
        try:
            async with pool.session("echo", server_config, timeout=10) as client:
                await client.call_tool("echo", {"text": "warm"})
                client._process.kill()
                await client._process.wait()

            async with pool.session("echo", server_config, timeout=10) as client:
                result = await client.call_tool("echo", {"text": "after"})
            return pool.spawn_count, result.text()
        finally:
            await pool.close_all()

    spawns, text = run(scenario())
    assert spawns == 2
    assert text == "after"


def test_protocol_error_drops_connection(server_config):
    async def scenario():
        pool = McpPool(idle_seconds=60)
        try:
            with pytest.raises(McpError):
                async with pool.session("echo", server_config, timeout=10) as client:
                    await client.call_tool("does-not-exist", {})

            # 出错后应当重连，而不是复用可能错位的连接
            async with pool.session("echo", server_config, timeout=10) as client:
                result = await client.call_tool("echo", {"text": "ok"})
            return pool.spawn_count, result.text()
        finally:
            await pool.close_all()

    spawns, text = run(scenario())
    assert spawns == 2
    assert text == "ok"


def test_reset_forces_reconnect(server_config):
    async def scenario():
        pool = McpPool(idle_seconds=60)
        try:
            async with pool.session("echo", server_config, timeout=10) as client:
                await client.call_tool("echo", {"text": "a"})
            await pool.reset("echo")
            async with pool.session("echo", server_config, timeout=10) as client:
                await client.call_tool("echo", {"text": "b"})
            return pool.spawn_count
        finally:
            await pool.close_all()

    assert run(scenario()) == 2


def test_api_calls_reuse_the_pooled_connection(client, agent, allow_echo):
    """走 API 也应该复用连接（第二次调用不再启动进程）。"""
    pool = get_mcp_pool()

    first = client.post(
        f"/api/v1/agents/{agent['id']}/tools/echo/invoke",
        json={"arguments": {"text": "warm"}},
    )
    assert first.json()["status"] == "succeeded"

    before = pool.spawn_count
    for index in range(2):
        response = client.post(
            f"/api/v1/agents/{agent['id']}/tools/echo/invoke",
            json={"arguments": {"text": f"again-{index}"}},
        )
        assert response.json()["status"] == "succeeded"

    assert pool.spawn_count == before

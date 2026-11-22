"""工具执行器单元测试。"""

import asyncio
import time

import pytest

from app.core.errors import AppError, ErrorCode
from app.models.entities import ToolDefinition
from app.services import tool_executor


def _tool(name: str, execution_type: str = "builtin", timeout: float = 5.0) -> ToolDefinition:
    # 不落库，只构造一个带属性的对象
    return ToolDefinition(name=name, execution_type=execution_type, timeout_seconds=timeout)


def run(coro):
    return asyncio.run(coro)


def test_echo_builtin():
    assert run(tool_executor.execute_tool(_tool("echo"), {"text": "hi"})) == {"text": "hi"}


def test_echo_defaults_to_empty_text():
    assert run(tool_executor.execute_tool(_tool("echo"), {})) == {"text": ""}


def test_current_time_builtin():
    result = run(tool_executor.execute_tool(_tool("current_time"), {}))
    assert result["utc"].endswith("Z") or "+00:00" in result["utc"]
    assert isinstance(result["epoch_ms"], int)


def test_registered_but_unimplemented_tool_cannot_execute():
    """注册一个工具定义不会产生执行能力——名字不在白名单里就是不行。"""
    with pytest.raises(AppError) as excinfo:
        run(tool_executor.execute_tool(_tool("shell"), {"cmd": "ls"}))

    assert excinfo.value.code == ErrorCode.TOOL_EXECUTION_FAILED


def test_executor_type_is_refused():
    with pytest.raises(AppError) as excinfo:
        run(tool_executor.execute_tool(_tool("echo", execution_type="executor"), {"text": "x"}))

    assert excinfo.value.code == ErrorCode.EXECUTOR_NOT_AVAILABLE
    assert excinfo.value.status_code == 501


def test_timeout_maps_to_tool_timeout(monkeypatch):
    def slow(arguments):
        time.sleep(0.2)
        return {"done": True}

    monkeypatch.setitem(tool_executor.BUILTIN_IMPLEMENTATIONS, "slow_tool", slow)

    with pytest.raises(AppError) as excinfo:
        run(tool_executor.execute_tool(_tool("slow_tool", timeout=0.01), {}))

    assert excinfo.value.code == ErrorCode.TOOL_TIMEOUT
    assert excinfo.value.status_code == 504


def test_implementation_crash_does_not_leak_details(monkeypatch):
    def boom(arguments):
        raise RuntimeError("super secret internal detail")

    monkeypatch.setitem(tool_executor.BUILTIN_IMPLEMENTATIONS, "boom_tool", boom)

    with pytest.raises(AppError) as excinfo:
        run(tool_executor.execute_tool(_tool("boom_tool"), {}))

    assert excinfo.value.code == ErrorCode.TOOL_EXECUTION_FAILED
    assert "secret" not in excinfo.value.message

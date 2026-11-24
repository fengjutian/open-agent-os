"""会话模式 Ask / Plan / Craft。

语义与 Octop 对齐（``octop_harness.middleware.conversation_mode``）：

- ``craft``（默认）：全量工具；
- ``ask``：只暴露只读工具，不允许任何写操作；
- ``plan``：同样只给只读工具，并要求模型先产出计划、然后停下等用户确认；
- 未知模式一律回落 ``craft``（宁可多给能力，也不要误判成受限模式）。

**与 Octop 的差异**：Octop 有文件读写工具，所以 plan 模式允许写
``plans/*.md``；我们目前没有写工具，因此 plan 与 ask 的可用工具集相同，
区别只在提示词（一个要求产出计划并停下，一个是问答）。

管理员可以把某条工具标记为 ``read_only``（``ToolDefinition.read_only``），
它就会在 ask / plan 下继续可见——这是给 MCP 工具准备的：注册时无法自动判断
副作用，必须由人显式声明。
"""

from __future__ import annotations

from typing import Any

MODES: tuple[str, ...] = ("ask", "plan", "craft")
DEFAULT_MODE = "craft"

# 内建工具里天然只读的那些（不写库、不改文件、不联网）
BUILTIN_READ_ONLY: frozenset[str] = frozenset({"current_time"})

MODE_HINTS: dict[str, str] = {
    "ask": (
        "You are in Ask mode: answer using read-only tools only. "
        "Do not modify state, do not call tools that could have side effects. "
        "If the user needs changes, tell them to switch to Craft mode."
    ),
    "plan": (
        "You are in Plan mode: explore with read-only tools, then produce a numbered, "
        "step-by-step plan. Do not execute it. End your reply by asking the user to "
        "confirm before any change is made."
    ),
    "craft": "",
}


def normalize_mode(value: str | None, *, default: str = DEFAULT_MODE) -> str:
    """未知 / 缺省 → default（默认 craft）。"""
    if value in MODES:
        return value  # type: ignore[return-value]
    return default if default in MODES else DEFAULT_MODE


def mode_hint(mode: str) -> str:
    return MODE_HINTS.get(normalize_mode(mode), "")


def is_read_only(mode: str) -> bool:
    return normalize_mode(mode) in {"ask", "plan"}


def filter_tool_specs(
    specs: list[dict[str, Any]],
    *,
    mode: str,
    read_only_names: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """按模式裁剪暴露给模型的工具。

    craft 原样返回；ask/plan 只保留只读工具。被裁掉的工具模型根本看不到，
    因此不会去请求——这比「让它请求然后拒绝」省一轮往返，也避免污染上下文。
    """
    if normalize_mode(mode) == "craft":
        return specs

    allowed = set(BUILTIN_READ_ONLY) | set(read_only_names)
    return [spec for spec in specs if spec["function"]["name"] in allowed]

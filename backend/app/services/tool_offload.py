"""大 payload 旁路（Artifact Offload）。

**问题**：工具返回的结果会整块进模型上下文，而上下文预算只有
`CONTEXT_MAX_CHARS`（默认 24000 字符）。一条 22KB 的搜索结果能吃掉 94% 的预算，
把前面的对话全部挤出去（`build_messages` 只能整条丢弃历史，切不出一条消息）。

**做法**：在「工具执行完、消息进模型前」把**数据面**（大 payload）与**控制面**
（状态 + 摘要 + 引用）分开。模型看到的是一个信封，不含数据本体；完整结果本来
就在 `ToolInvocation.result` 里，没有丢——模型可以用 `artifact.read` 按需取回。

设计稿见 `docs/features/tool-result-offload.md`。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.telemetry import span
from app.models.entities import ToolDefinition, ToolInvocation

logger = logging.getLogger(__name__)

# 取回旁路结果的内建工具名。
ARTIFACT_READ_TOOL_NAME = "artifact.read"

# 平台内建只读工具：没有策略规则命中时默认放行（显式 deny 仍然生效）。
DEFAULT_ALLOWED_TOOLS = frozenset({ARTIFACT_READ_TOOL_NAME})

ARTIFACT_READ_DESCRIPTION = "读取被旁路的大工具结果的一段切片（按字符偏移取）"

ARTIFACT_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "invocation_id": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1},
    },
    "required": ["invocation_id"],
    "additionalProperties": False,
}


def serialize_result(result: Any) -> str | None:
    """结果的可序列化文本；不可序列化返回 None（不旁路，保持现状）。"""
    if result is None:
        return None
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def should_offload(*, status: str, serialized: str | None) -> bool:
    """是否剥离。条件全部满足才剥离，任一不满足保持现状。

    - 只剥离成功的结果：失败与被拒的信息通常很小，而且对模型判断很重要；
    - 不可序列化的结果不剥离：拿不到稳定的切片就没必要引用。
    """
    if not settings.tool_result_offload_enabled:
        return False
    if status != "succeeded":
        return False
    if serialized is None:
        return False
    return len(serialized) >= settings.tool_result_offload_chars


def build_envelope(*, invocation_id: str, serialized: str) -> dict[str, Any]:
    """模型侧看到的信封：状态 + 摘要 + 引用，**不含数据本体**。

    保留 preview 与 chars，是为了让模型判断「这份结果值不值得再读一次」，
    而不是盲目调用取回工具。
    """
    return {
        "status": "succeeded",
        "result_omitted": True,
        "artifact": {
            "invocation_id": invocation_id,
            "chars": len(serialized),
            "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "preview": serialized[: settings.tool_result_preview_chars],
        },
        "hint": (
            "结果过大已旁路。需要细节时调用 artifact.read(invocation_id, offset, limit)，"
            "不要凭 preview 猜测。"
        ),
    }


def read_slice(
    db: Session,
    *,
    invocation_id: str,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """取回一次调用结果的切片。

    租户隔离靠 ORM 层强制：`ToolInvocation` 属于 `TenantScoped`，
    别的租户的 invocation_id 查不到（自然变成 404）。
    """
    if not invocation_id:
        raise AppError(
            ErrorCode.TOOL_ARGUMENTS_INVALID,
            "artifact.read 需要 invocation_id",
            status_code=422,
        )

    invocation = db.scalar(
        select(ToolInvocation).where(ToolInvocation.id == invocation_id)
    )
    if invocation is None:
        raise AppError(
            ErrorCode.TOOL_NOT_FOUND,
            f"调用记录 {invocation_id} 不存在",
            status_code=404,
        )

    serialized = serialize_result(invocation.result)
    if serialized is None:
        raise AppError(
            ErrorCode.TOOL_EXECUTION_FAILED,
            "该调用没有可读取的结果",
            status_code=409,
        )

    offset = max(0, int(offset or 0))
    # 单次上限与旁路阈值一致：避免"读回来又爆"
    cap = max(1, min(int(limit or settings.tool_result_offload_chars), settings.tool_result_offload_chars))
    chunk = serialized[offset : offset + cap]
    return {
        "invocation_id": invocation.id,
        "tool_name": invocation.tool_name,
        "offset": offset,
        "limit": cap,
        "total_chars": len(serialized),
        "text": chunk,
        "eof": offset + len(chunk) >= len(serialized),
    }


def ensure_artifact_read_tool(db: Session) -> None:
    """确保平台内建的 `artifact.read` 已在**当前租户**的工具注册表里。

    这是平台能力而不是用户注册的工具：注册表里没有它，网关的第一步
    （工具存在性校验）就会把调用挡掉。注册后仍然要过 schema、策略与审计。

    已存在就不动——管理员显式停用它（enabled=false）时不该被重新建回来。
    用 savepoint 包住插入：并发首次调用时唯一约束冲突不会毁掉外层事务。
    """
    existing = db.scalar(
        select(ToolDefinition).where(ToolDefinition.name == ARTIFACT_READ_TOOL_NAME)
    )
    if existing is not None:
        return
    try:
        with db.begin_nested():
            db.add(
                ToolDefinition(
                    name=ARTIFACT_READ_TOOL_NAME,
                    description=ARTIFACT_READ_DESCRIPTION,
                    input_schema=ARTIFACT_READ_SCHEMA,
                    execution_type="builtin",
                    enabled=True,
                    # 只读：ask / plan 模式下也应当可见
                    read_only=True,
                    timeout_seconds=10.0,
                )
            )
    except IntegrityError:
        # 并发下另一个请求已经建好了，这里直接用现成的
        logger.debug("artifact.read already registered by another request")


def offload_span(tool_name: str, chars: int) -> None:
    """旁路发生时留一条 span，便于在链路里看到上下文被瘦身了。"""
    with span("tool.result.offload", **{"tool.name": tool_name}) as current:
        current.set_attribute("artifact.offloaded", True)
        current.set_attribute("artifact.chars", chars)


def artifact_read_spec() -> dict[str, Any]:
    """artifact.read 给模型看的工具规格。"""
    return {
        "type": "function",
        "function": {
            "name": ARTIFACT_READ_TOOL_NAME,
            "description": ARTIFACT_READ_DESCRIPTION,
            "parameters": ARTIFACT_READ_SCHEMA,
        },
    }

"""长期记忆：跨会话保存"值得记住的事"，并按相关性注入上下文。

**检索用词法，不用向量。** 理由：接向量就要接 embedding 服务（模型下载或云 API），
在自托管场景里是一个额外依赖；而"用户偏好、项目约定、已确认的结论"这类记忆通常
有明确的关键词。中文没有空格，这里用「字符二元组 + 英文单词」做召回，足够覆盖
大多数场景，且完全确定性、可测试。

边界写清楚：这是**词法召回**，不是语义检索。"用户喜欢深色主题"和"他要暗色 UI"
不会互相命中——要那种能力，得换成向量，接口不用改。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Memory
from app.services.model_gateway import ModelProvider

logger = logging.getLogger(__name__)

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASCII_WORD = re.compile(r"[a-z0-9_]{2,}")

DISTILL_SYSTEM_PROMPT = (
    "你是一个记忆提炼器。从给定对话里提取**值得长期记住**的事实，"
    "例如用户偏好、项目约定、明确结论、重要的外部约束。\n"
    "要求：\n"
    "1. 只提取确定性信息，不要猜测；\n"
    "2. 不要提取一次性的闲聊或临时任务；\n"
    "3. 每条一句话，独立可读，不要出现「他/它」这类指代；\n"
    "4. 最多 {limit} 条。\n"
    '只输出 JSON 数组，形如 [{{"content": "...", "tags": ["偏好"]}}]，不要额外文字。'
)


def tokens(text: str) -> set[str]:
    """英文按词、中文按字符二元组切分。"""
    lowered = (text or "").lower()
    words = set(_ASCII_WORD.findall(lowered))
    chars = _CJK.findall(lowered)
    bigrams = {f"{chars[index]}{chars[index + 1]}" for index in range(len(chars) - 1)}
    # 单字也保留：查询很短时（如"预算"）二元组只有一个，单字能提高召回
    return words | bigrams | set(chars)


def score(query_tokens: set[str], content: str) -> float:
    if not query_tokens:
        return 0.0
    memory_tokens = tokens(content)
    if not memory_tokens:
        return 0.0
    hit = len(query_tokens & memory_tokens)
    return hit / len(query_tokens)


def search_memories(
    db: Session,
    query: str,
    *,
    agent_id: str | None = None,
    limit: int = 5,
    min_score: float = 0.12,
) -> list[Memory]:
    """按词法相关度召回。agent 级记忆与租户级记忆都参与。"""
    query_tokens = tokens(query)
    if not query_tokens:
        return []

    statement = select(Memory)
    if agent_id is not None:
        # 租户级（agent_id 为空）与当前 Agent 的记忆都算
        statement = statement.where((Memory.agent_id == agent_id) | (Memory.agent_id.is_(None)))
    rows = db.scalars(statement).all()

    scored = [(score(query_tokens, row.content), row) for row in rows]
    ranked = sorted(
        ((value, row) for value, row in scored if value >= min_score),
        key=lambda item: (-item[0], item[1].created_at),
    )
    return [row for _, row in ranked[:limit]]


def format_memory_block(memories: list[Memory]) -> str:
    if not memories:
        return ""
    lines = [f"- {item.content}" for item in memories]
    return "[相关记忆]\n" + "\n".join(lines)


def retrieve_block(
    db: Session,
    query: str,
    *,
    agent_id: str | None,
    limit: int,
) -> str:
    """给运行用的便捷入口：召回 + 格式化，失败一律降级为"没有记忆"。"""
    try:
        memories = search_memories(db, query, agent_id=agent_id, limit=limit)
    except Exception:  # noqa: BLE001 - 记忆检索失败不该拖垮对话
        logger.warning("memory retrieval failed", exc_info=True)
        return ""
    return format_memory_block(memories)


async def distill_conversation(
    provider: ModelProvider,
    *,
    model: str,
    transcript: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """把一段对话提炼成若干条记忆（由调用方负责落库）。"""
    reply = await provider.generate(
        model=model,
        messages=[
            {"role": "system", "content": DISTILL_SYSTEM_PROMPT.format(limit=limit)},
            {"role": "user", "content": transcript[:20000]},
        ],
        tools=None,
    )
    text = (reply.text or "").strip()
    if not text:
        return []

    # 容忍模型把 JSON 包在 ```json ... ``` 里
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        import json

        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("memory distill returned non-JSON", extra={"fields": {"preview": text[:200]}})
        return []

    if not isinstance(parsed, list):
        return []

    results: list[dict[str, Any]] = []
    for item in parsed[:limit]:
        if isinstance(item, str):
            content = item.strip()
            tags: list[str] = []
        elif isinstance(item, dict):
            content = str(item.get("content") or "").strip()
            raw_tags = item.get("tags") or []
            tags = [str(tag)[:40] for tag in raw_tags if isinstance(tag, str | int)][:10]
        else:
            continue
        if content:
            results.append({"content": content[:1000], "tags": tags})
    return results

"""上下文窗口管理。

V0.1 之前把整个会话历史原样发给模型：历史一长就会超出模型窗口导致 400，
而且成本随轮次线性增长。这里按字符预算从**最新消息往回**装载，超出的旧消息丢弃。

为什么用字符而不是 token：避免引入 tiktoken 这类与具体模型绑定的依赖。
字符预算是保守的近似，跨 provider/model 都可控；后续接真实计费时再换成
按模型 tokenizer 计费。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

# 每条消息的 role/分隔符等结构开销（保守估计）。
_PER_MESSAGE_OVERHEAD = 8


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ContextResult:
    messages: list[ChatMessage] = field(default_factory=list)
    dropped_messages: int = 0
    used_chars: int = 0
    truncated_newest: bool = False


def build_messages(
    system_prompt: str,
    history: Sequence[ChatMessage],
    max_chars: int,
) -> ContextResult:
    """在 max_chars 字符预算内构造 [system, ...history] 消息列表。

    - system prompt 永远保留（预算不足时退化为截断它，而不是丢弃）。
    - 历史从最新往回装；装不下的旧消息整条丢弃。
    - 若最新一条本身就超预算，对其内容做尾部截断，保证请求至少带上当前输入。
    """
    max_chars = max(1, max_chars)
    budget = max_chars - len(system_prompt)

    if budget <= 0:
        return ContextResult(
            messages=[ChatMessage(role="system", content=system_prompt[-max_chars:])],
            dropped_messages=len(history),
            used_chars=max_chars,
            truncated_newest=True,
        )

    selected: list[ChatMessage] = []
    used = 0
    truncated_newest = False

    for message in reversed(history):
        cost = len(message.content) + _PER_MESSAGE_OVERHEAD
        if used + cost <= budget:
            selected.append(message)
            used += cost
            continue

        if not selected:
            room = budget - _PER_MESSAGE_OVERHEAD
            if room > 0:
                selected.append(ChatMessage(role=message.role, content=message.content[-room:]))
                used = budget
                truncated_newest = True
        break

    selected.reverse()
    return ContextResult(
        messages=[ChatMessage(role="system", content=system_prompt), *selected],
        dropped_messages=len(history) - len(selected),
        used_chars=len(system_prompt) + used,
        truncated_newest=truncated_newest,
    )

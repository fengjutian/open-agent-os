"""把过长的对话历史压成摘要。

为什么需要：`build_messages` 是按整条消息丢弃的——历史一长，最老的消息会整段消失，
用户会感觉"Agent 失忆"。摘要替代丢弃：把已经装不下的那部分历史压成一段要点，
与最近若干条消息一起送进模型。

三个刻意的约束：

1. **只在真的装不下时才摘要**（按字符预算的比例触发），否则每次对话都多一次模型调用；
2. **摘要失败不影响主流程**：退回原来的"整条丢弃"，宁可忘事也不能让对话失败；
3. **摘要随会话持久化**：`summary_message_count` 记录已经覆盖到第几条历史，
   下一轮只摘要新增的部分，不会重复花钱。
"""

from __future__ import annotations

import logging

from app.services.context import ChatMessage
from app.services.model_gateway import ModelProvider

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM_PROMPT = (
    "你是一个对话摘要器。把给定的对话压缩成要点，供后续轮次参考。\n"
    "要求：\n"
    "1. 保留事实、数字、结论、用户偏好与承诺；\n"
    "2. 保留未完成的事项与它们的下一步；\n"
    "3. 不要编造对话里没有的信息；\n"
    "4. 用条目式短句，总长度不超过 {max_chars} 字。"
)


async def summarize_history(
    provider: ModelProvider,
    *,
    model: str,
    messages: list[ChatMessage],
    previous_summary: str | None,
    max_chars: int,
) -> str:
    """把一批消息（若已有旧摘要则合并）压成新的摘要。"""
    transcript = "\n".join(f"{message.role}：{message.content}" for message in messages)
    parts = []
    if previous_summary:
        parts.append(f"已有摘要：\n{previous_summary}")
    parts.append(f"需要压缩的新对话：\n{transcript}")
    if len(parts[-1]) > max_chars * 6:
        # 输入本身过大时先截断，避免摘要请求自己就超预算
        parts[-1] = parts[-1][: max_chars * 6]

    reply = await provider.generate(
        model=model,
        messages=[
            {
                "role": "system",
                "content": SUMMARY_SYSTEM_PROMPT.format(max_chars=max_chars),
            },
            {"role": "user", "content": "\n\n".join(parts)},
        ],
        tools=None,
    )
    text = (reply.text or "").strip()
    if not text:
        raise ValueError("摘要模型返回了空内容")
    return text[:max_chars]

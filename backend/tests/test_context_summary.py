"""上下文压缩：历史装不下时用摘要替代整条丢弃。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.session import SessionLocal
from app.models.entities import Conversation, Message
from app.services import agent_runtime as agent_runtime_module
from app.services.context import ChatMessage, build_messages
from app.services.model_gateway import ModelProvider, ModelReply, StreamDone

SUMMARY_MARK = "对话摘要器"


class SummaryAwareProvider(ModelProvider):
    """识别"是否在写摘要"，分别返回摘要与正常回答，并记录每次调用。"""

    def __init__(self, *, fail_on_summary: bool = False) -> None:
        self.calls: list[list[dict]] = []
        self.fail_on_summary = fail_on_summary

    @staticmethod
    def _is_summary(messages: list[dict]) -> bool:
        return any(SUMMARY_MARK in str(m.get("content") or "") for m in messages)

    async def generate(self, *, model, messages, tools=None):
        self.calls.append(list(messages))
        if self._is_summary(messages):
            if self.fail_on_summary:
                raise RuntimeError("摘要模型炸了")
            return ModelReply(text="摘要：用户在讨论一个很长的话题，结论是 X。")
        return ModelReply(text="这是正常回答")

    async def stream(self, *, model, messages, tools=None):
        reply = await self.generate(model=model, messages=messages, tools=tools)
        yield StreamDone(reply=reply)

    @property
    def summary_calls(self) -> int:
        return sum(1 for call in self.calls if self._is_summary(call))


def _use(monkeypatch, provider) -> None:
    monkeypatch.setattr(agent_runtime_module, "get_model_provider", lambda: provider)


def _seed_history(conversation_id: str, *, rounds: int = 10, size: int = 3000) -> None:
    """塞一段足够长的历史，超过上下文预算的触发线。"""
    now = datetime.now(UTC)
    with SessionLocal() as db:
        for index in range(rounds):
            for role in ("user", "assistant"):
                db.add(
                    Message(
                        conversation_id=conversation_id,
                        role=role,
                        content=f"{role}-{index} " + "内容" * (size // 2),
                        created_at=now + timedelta(seconds=index * 2 + (role == "assistant")),
                    )
                )
        db.commit()


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_build_messages_includes_summary_in_system_prompt():
    context = build_messages(
        "你是助手",
        [ChatMessage(role="user", content="最近一句")],
        1000,
        summary="早前聊了什么",
    )
    system = context.messages[0]
    assert system.role == "system"
    assert "你是助手" in system.content
    assert "[早前对话摘要]" in system.content
    assert "早前聊了什么" in system.content


def test_build_messages_without_summary_is_unchanged():
    context = build_messages("你是助手", [ChatMessage(role="user", content="hi")], 1000)
    assert "[早前对话摘要]" not in context.messages[0].content


# ---------------------------------------------------------------------------
# 运行时行为
# ---------------------------------------------------------------------------


def _send(client, conversation_id: str, text: str = "继续"):
    return client.post(f"/api/v1/conversations/{conversation_id}/messages", json={"content": text})


def test_long_history_is_summarized(client, agent, conversation, monkeypatch):
    _seed_history(conversation["id"])
    provider = SummaryAwareProvider()
    _use(monkeypatch, provider)

    response = _send(client, conversation["id"])
    assert response.status_code == 200, response.text
    assert response.json()["assistant_message"]["content"] == "这是正常回答"

    # 至少有一次摘要调用，且摘要被写进会话
    assert provider.summary_calls == 1
    with SessionLocal() as db:
        row = db.get(Conversation, conversation["id"])
        assert row.summary
        assert row.summary_message_count > 0

    # 真实的回答请求里带上了摘要
    answer_call = next(call for call in provider.calls if not provider._is_summary(call))
    assert "[早前对话摘要]" in answer_call[0]["content"]


def test_summary_is_reused_and_not_recomputed(client, agent, conversation, monkeypatch):
    _seed_history(conversation["id"])
    provider = SummaryAwareProvider()
    _use(monkeypatch, provider)

    _send(client, conversation["id"])
    assert provider.summary_calls == 1

    # 第二轮只多了一条新消息，不该再触发摘要
    _send(client, conversation["id"], "再说一句")
    assert provider.summary_calls == 1

    # 但摘要仍然被带进上下文
    last_answer_call = [call for call in provider.calls if not provider._is_summary(call)][-1]
    assert "[早前对话摘要]" in last_answer_call[0]["content"]


def test_short_history_is_not_summarized(client, agent, conversation, monkeypatch):
    provider = SummaryAwareProvider()
    _use(monkeypatch, provider)

    _send(client, conversation["id"])
    assert provider.summary_calls == 0

    with SessionLocal() as db:
        assert db.get(Conversation, conversation["id"]).summary is None


def test_summary_failure_falls_back_to_truncation(client, agent, conversation, monkeypatch):
    _seed_history(conversation["id"])
    provider = SummaryAwareProvider(fail_on_summary=True)
    _use(monkeypatch, provider)

    response = _send(client, conversation["id"])
    # 摘要失败不影响对话本身
    assert response.status_code == 200, response.text
    assert response.json()["assistant_message"]["content"] == "这是正常回答"

    with SessionLocal() as db:
        assert db.get(Conversation, conversation["id"]).summary is None


@pytest.mark.parametrize("disabled", [True])
def test_summary_can_be_disabled(client, agent, conversation, monkeypatch, disabled):
    from app.core.config import settings

    _seed_history(conversation["id"])
    monkeypatch.setattr(settings, "context_summary_enabled", not disabled)
    provider = SummaryAwareProvider()
    _use(monkeypatch, provider)

    _send(client, conversation["id"])
    assert provider.summary_calls == 0

"""上下文窗口管理（纯函数）单元测试。"""

from app.services.context import ChatMessage, build_messages


def _history(count: int, size: int = 10) -> list[ChatMessage]:
    roles = ["user", "assistant"]
    return [
        ChatMessage(role=roles[i % 2], content=f"m{i}" + "x" * size)
        for i in range(count)
    ]


def test_keeps_everything_when_within_budget():
    history = _history(3)
    result = build_messages("sys", history, max_chars=10_000)

    assert result.dropped_messages == 0
    assert not result.truncated_newest
    assert result.messages[0] == ChatMessage(role="system", content="sys")
    assert len(result.messages) == 4


def test_drops_oldest_messages_when_over_budget():
    history = _history(50)
    result = build_messages("sys", history, max_chars=200)

    assert result.dropped_messages > 0
    # 保留的必须是**最新**的连续片段
    assert result.messages[-1].content == history[-1].content
    assert result.used_chars <= 200
    assert len(result.messages) == 1 + (len(history) - result.dropped_messages)


def test_newest_message_is_kept_and_truncated_when_alone_too_large():
    history = [ChatMessage(role="user", content="y" * 5_000)]
    result = build_messages("sys", history, max_chars=100)

    assert result.truncated_newest is True
    assert result.dropped_messages == 0
    assert len(result.messages) == 2
    assert len(result.messages[-1].content) < 5_000
    assert result.used_chars <= 100


def test_system_prompt_is_always_first():
    result = build_messages("you are helpful", _history(5), max_chars=1_000)
    assert result.messages[0].role == "system"
    assert result.messages[0].content == "you are helpful"


def test_system_prompt_is_truncated_when_budget_smaller_than_it():
    long_prompt = "p" * 500
    result = build_messages(long_prompt, _history(3), max_chars=100)

    assert len(result.messages) == 1
    assert result.messages[0].role == "system"
    assert len(result.messages[0].content) == 100
    assert result.dropped_messages == 3


def test_empty_history_yields_system_only():
    result = build_messages("sys", [], max_chars=100)
    assert result.messages == [ChatMessage(role="system", content="sys")]
    assert result.dropped_messages == 0

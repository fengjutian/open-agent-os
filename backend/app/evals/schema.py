"""评测套件的文件格式。

套件是纯 JSON，便于进版本控制、也便于人读。一个用例 = 一次「发消息 + 断言」。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Expect(BaseModel):
    """对一次运行的断言。全部满足才算通过。"""

    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)
    regex: str | None = None
    run_status: str | None = None
    min_tool_calls: int | None = None
    max_tool_calls: int | None = None
    max_duration_ms: float | None = None


class MockToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class EvalTool(BaseModel):
    """套件需要的工具定义，跑之前会确保已注册。"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    execution_type: str = "builtin"


class EvalCase(BaseModel):
    name: str
    prompt: str
    system_prompt: str = "You are a helpful assistant."

    # 指定 mock_reply 时使用确定性 provider，评测不依赖真实模型，可在 CI 里跑。
    mock_reply: str | None = None
    # 指定时先让模型请求一次工具调用，再给出最终回答。
    mock_tool_call: MockToolCall | None = None
    final_reply: str = "已完成"

    # 每个用例独占一个 Agent，可通过策略规则预置授权
    allow_tools: list[str] = Field(default_factory=list)

    expect: Expect = Field(default_factory=Expect)


class EvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    tools: list[EvalTool] = Field(default_factory=list)
    cases: list[EvalCase]

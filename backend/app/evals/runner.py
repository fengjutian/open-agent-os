"""评测执行器。

被测对象是**平台本身**：鉴权、上下文拼装、工具治理、错误映射、运行记录。
用例可以指定 `mock_reply` / `mock_tool_call` 使用确定性 provider，
这样 CI 里跑不会因模型随机性抖动；不指定时就走配置里的真实模型。

跑法：

    python -m app.evals --suite evals/baseline.json
    python -m app.evals --suite evals/tools.json --json report.json

退出码：全部通过 0，有失败 1（可直接用于 CI）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.evals.schema import EvalCase, EvalSuite
from app.services import agent_runtime
from app.services.model_gateway import ModelProvider, ModelReply, StreamDone, ToolCall


class _StaticProvider(ModelProvider):
    def __init__(self, text: str) -> None:
        self._text = text

    async def stream(self, *, model, messages, tools=None):
        yield StreamDone(reply=ModelReply(text=self._text))


class _ToolCallThenReplyProvider(ModelProvider):
    """第一次请求工具，第二次给出最终回答。"""

    def __init__(self, call: ToolCall, final: str) -> None:
        self._call = call
        self._final = final
        self._first = True

    async def stream(self, *, model, messages, tools=None):
        if self._first:
            self._first = False
            yield StreamDone(reply=ModelReply(text="", tool_calls=(self._call,)))
            return
        yield StreamDone(reply=ModelReply(text=self._final))


@dataclass
class CaseResult:
    name: str
    passed: bool
    duration_ms: float
    failures: list[str] = field(default_factory=list)
    answer: str = ""
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "duration_ms": round(self.duration_ms, 1),
            "failures": self.failures,
            "answer": self.answer,
            "run_id": self.run_id,
        }


@dataclass
class SuiteReport:
    suite: str
    results: list[CaseResult] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "total": len(self.results),
            "passed": self.passed,
            "failed": self.failed,
            "duration_ms": round(self.duration_ms, 1),
            "results": [result.to_dict() for result in self.results],
        }


def load_suite(path: str | Path) -> EvalSuite:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return EvalSuite.model_validate(payload)


def _provider_for(case: EvalCase) -> ModelProvider | None:
    if case.mock_tool_call is not None:
        return _ToolCallThenReplyProvider(
            ToolCall(
                id="eval_call_1",
                name=case.mock_tool_call.name,
                arguments=case.mock_tool_call.arguments,
            ),
            case.final_reply,
        )
    if case.mock_reply is not None:
        return _StaticProvider(case.mock_reply)
    return None


def _ensure_tools(client, suite: EvalSuite) -> None:
    for tool in suite.tools:
        response = client.post(
            "/api/v1/tools",
            json={
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "execution_type": tool.execution_type,
            },
        )
        # 已注册过不算失败（重复跑同一套件）
        if response.status_code not in (201, 409):
            raise RuntimeError(f"注册工具 {tool.name} 失败: {response.status_code} {response.text}")


def _evaluate(case: EvalCase, *, answer: str, run: dict[str, Any], duration_ms: float) -> list[str]:
    failures: list[str] = []
    expect = case.expect

    for needle in expect.contains:
        if needle not in answer:
            failures.append(f"回答中缺少 {needle!r}")
    for needle in expect.not_contains:
        if needle in answer:
            failures.append(f"回答中不应出现 {needle!r}")
    if expect.regex and re.search(expect.regex, answer) is None:
        failures.append(f"回答不匹配正则 {expect.regex!r}")

    if expect.run_status and run.get("status") != expect.run_status:
        failures.append(f"运行状态是 {run.get('status')!r}，期望 {expect.run_status!r}")

    tool_calls = int(run.get("tool_calls", 0))
    if expect.min_tool_calls is not None and tool_calls < expect.min_tool_calls:
        failures.append(f"工具调用 {tool_calls} 次，少于期望的 {expect.min_tool_calls}")
    if expect.max_tool_calls is not None and tool_calls > expect.max_tool_calls:
        failures.append(f"工具调用 {tool_calls} 次，多于期望的 {expect.max_tool_calls}")

    if expect.max_duration_ms is not None and duration_ms > expect.max_duration_ms:
        failures.append(f"耗时 {duration_ms:.0f}ms 超过上限 {expect.max_duration_ms:.0f}ms")

    return failures


def run_case(client, case: EvalCase, *, suite_name: str) -> CaseResult:
    started = time.perf_counter()

    agent = client.post(
        "/api/v1/agents",
        json={"name": f"eval:{suite_name}:{case.name}", "system_prompt": case.system_prompt},
    ).json()
    for tool_name in case.allow_tools:
        client.post(
            "/api/v1/policy-rules",
            json={"agent_id": agent["id"], "tool_name": tool_name, "effect": "allow", "priority": 100},
        )
    conversation = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={"title": case.name}
    ).json()

    provider = _provider_for(case)
    original = agent_runtime.get_model_provider
    if provider is not None:
        # 评测/测试用的注入点：只为这一个用例替换 provider
        agent_runtime.get_model_provider = lambda: provider
    try:
        send_started = time.perf_counter()
        response = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={"content": case.prompt},
        )
        send_duration_ms = (time.perf_counter() - send_started) * 1000
    finally:
        agent_runtime.get_model_provider = original

    duration_ms = (time.perf_counter() - started) * 1000

    if response.status_code != 200:
        return CaseResult(
            name=case.name,
            passed=False,
            duration_ms=duration_ms,
            failures=[f"请求失败: {response.status_code} {response.text[:200]}"],
        )

    body = response.json()
    answer = body["assistant_message"]["content"]
    run = client.get(f"/api/v1/runs/{body['run_id']}").json()
    failures = _evaluate(
        case, answer=answer, run=run, duration_ms=send_duration_ms
    )
    return CaseResult(
        name=case.name,
        passed=not failures,
        duration_ms=duration_ms,
        failures=failures,
        answer=answer,
        run_id=body["run_id"],
    )


def run_suite(client, suite: EvalSuite) -> SuiteReport:
    _ensure_tools(client, suite)
    report = SuiteReport(suite=suite.name)
    started = time.perf_counter()
    for case in suite.cases:
        report.results.append(run_case(client, case, suite_name=suite.name))
    report.duration_ms = (time.perf_counter() - started) * 1000
    return report

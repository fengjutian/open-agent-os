"""评测执行器与 CLI。"""

import json
from pathlib import Path

from app.evals.__main__ import main
from app.evals.runner import load_suite, run_suite
from app.evals.schema import EvalSuite

BACKEND_ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = BACKEND_ROOT / "evals"


def test_load_suite():
    suite = load_suite(EVALS_DIR / "baseline.json")

    assert suite.name == "baseline"
    assert len(suite.cases) == 3
    assert suite.cases[0].expect.contains


def test_baseline_suite_passes(client):
    report = run_suite(client, load_suite(EVALS_DIR / "baseline.json"))

    assert report.failed == 0, [result.to_dict() for result in report.results]
    assert report.passed == 3


def test_tools_suite_passes(client):
    report = run_suite(client, load_suite(EVALS_DIR / "tools.json"))

    assert report.failed == 0, [result.to_dict() for result in report.results]


def test_failed_expectation_is_reported(client):
    suite = EvalSuite.model_validate(
        {
            "name": "failing",
            "cases": [
                {
                    "name": "永远失败",
                    "prompt": "x",
                    "mock_reply": "实际回答",
                    "expect": {"contains": ["不存在的词"]},
                }
            ],
        }
    )

    report = run_suite(client, suite)

    assert report.failed == 1
    assert "不存在的词" in report.results[0].failures[0]


def test_tool_call_expectations_are_checked(client):
    suite = EvalSuite.model_validate(
        {
            "name": "tool-expect",
            "tools": [
                {
                    "name": "echo",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                }
            ],
            "cases": [
                {
                    "name": "期望有工具调用",
                    "prompt": "hi",
                    "mock_tool_call": {"name": "echo", "arguments": {"text": "hi"}},
                    "final_reply": "done",
                    "expect": {"min_tool_calls": 1},
                }
            ],
        }
    )

    report = run_suite(client, suite)
    assert report.failed == 0, [result.to_dict() for result in report.results]


def test_report_serializes(client):
    report = run_suite(client, load_suite(EVALS_DIR / "baseline.json"))
    payload = report.to_dict()

    json.dumps(payload)  # 必须可 JSON 序列化
    assert payload["total"] == 3
    assert payload["failed"] == 0
    assert payload["results"][0]["run_id"]


def test_cli_returns_zero_on_success():
    assert main(["--suite", str(EVALS_DIR / "baseline.json"), "--quiet"]) == 0


def test_cli_returns_one_on_failure(tmp_path):
    path = tmp_path / "fail.json"
    path.write_text(
        json.dumps(
            {
                "name": "failing",
                "cases": [
                    {
                        "name": "nope",
                        "prompt": "x",
                        "mock_reply": "a",
                        "expect": {"contains": ["zzz"]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["--suite", str(path), "--quiet"]) == 1


def test_cli_writes_json_report(tmp_path):
    report_path = tmp_path / "report.json"

    code = main(
        [
            "--suite",
            str(EVALS_DIR / "baseline.json"),
            "--suite",
            str(EVALS_DIR / "tools.json"),
            "--json",
            str(report_path),
            "--quiet",
        ]
    )

    assert code == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    # 重复传 --suite 必须累积，而不是后者覆盖前者
    assert [entry["suite"] for entry in payload] == ["baseline", "tools"]
    assert all(entry["failed"] == 0 for entry in payload)

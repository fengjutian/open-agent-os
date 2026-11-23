"""OpenTelemetry：span 树、属性、指标与日志关联。

全部用内存导出器断言，不需要 Jaeger / Collector。
"""

import json
import logging

from app.core.logging import JsonFormatter
from app.core.telemetry import span

NAMES = {
    "http.server",
    "agent.run",
    "model.request",
    "tool.call",
}


def _spans_by_name(span_exporter, name: str) -> list:
    return [item for item in span_exporter.get_finished_spans() if item.name == name]


def test_http_and_run_spans_are_recorded(client, conversation, span_exporter):
    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"}
    )
    assert response.status_code == 200

    names = {item.name for item in span_exporter.get_finished_spans()}
    assert {"http.server", "agent.run", "model.request"} <= names

    http_span = _spans_by_name(span_exporter, "http.server")[-1]
    assert http_span.attributes["http.status_code"] == 200
    assert http_span.attributes["http.method"] == "POST"

    run_span = _spans_by_name(span_exporter, "agent.run")[-1]
    assert run_span.attributes["tenant.id"] == "default"
    assert run_span.attributes["run.status"] == "succeeded"
    assert run_span.attributes["model"]


def test_agent_run_is_child_of_http_span(client, conversation, span_exporter):
    client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"})

    run_span = _spans_by_name(span_exporter, "agent.run")[-1]
    http_span = _spans_by_name(span_exporter, "http.server")[-1]

    assert run_span.parent is not None
    assert run_span.parent.span_id == http_span.context.span_id
    # 同一个 trace
    assert run_span.context.trace_id == http_span.context.trace_id


def test_model_request_span_is_child_of_run(client, conversation, span_exporter):
    client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"})

    run_span = _spans_by_name(span_exporter, "agent.run")[-1]
    model_span = _spans_by_name(span_exporter, "model.request")[-1]

    assert model_span.parent.span_id == run_span.context.span_id
    assert model_span.attributes["iteration"] == 1


def test_tool_span_is_recorded(client, agent, allow_echo, span_exporter):
    response = client.post(
        f"/api/v1/agents/{agent['id']}/tools/echo/invoke",
        json={"arguments": {"text": "hi"}},
    )
    assert response.json()["status"] == "succeeded"

    span_item = _spans_by_name(span_exporter, "tool.call")[-1]
    assert span_item.attributes["tool.name"] == "echo"
    assert span_item.attributes["tool.status"] == "succeeded"
    assert span_item.attributes["tool.allowed"] is True


def test_denied_tool_still_produces_a_span(client, agent, echo_tool, span_exporter):
    body = client.post(
        f"/api/v1/agents/{agent['id']}/tools/echo/invoke",
        json={"arguments": {"text": "hi"}},
    ).json()
    assert body["status"] == "denied"

    span_item = _spans_by_name(span_exporter, "tool.call")[-1]
    assert span_item.attributes["tool.status"] == "denied"
    assert span_item.attributes["tool.allowed"] is False


def test_metrics_are_recorded(client, conversation, metric_reader):
    client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"content": "hi"})

    data = metric_reader.get_metrics_data()
    names = set()
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                names.add(metric.name)

    assert "agent.runs" in names
    assert "agent.run.duration_ms" in names


def test_logs_carry_trace_ids(client, conversation, span_exporter):
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="inside span", args=(), exc_info=None,
    )

    with span("test.span"):
        payload = json.loads(formatter.format(record))

    assert payload["trace_id"]
    assert payload["span_id"]
    assert len(payload["trace_id"]) == 32


def test_logs_without_span_have_no_trace_id():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="outside span", args=(), exc_info=None,
    )

    payload = json.loads(formatter.format(record))
    assert "trace_id" not in payload

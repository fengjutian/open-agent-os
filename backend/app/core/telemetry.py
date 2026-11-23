"""OpenTelemetry：链路、指标与日志关联。

三条设计取舍：

1. **埋点无条件存在**：没有配置 provider 时 OTel API 返回 no-op，成本接近于零，
   所以业务代码里不写 `if settings.otel_enabled`，避免埋点被条件分支腐化。
2. **默认关闭**：`OTEL_ENABLED=false` 时不初始化任何 provider，本地开发与既有测试
   不受影响；需要时再配 `OTEL_EXPORTER_OTLP_ENDPOINT`。
3. **导出器可注入**：测试用 `InMemorySpanExporter` / `InMemoryMetricReader`
   就能断言整棵 span 树，不需要部署 Jaeger 或 Collector。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import format_span_id, format_trace_id

from app.core.config import settings

logger = logging.getLogger(__name__)

SERVICE_VERSION = "0.1.0"
_TRACER_NAME = "agent_platform"

_initialized = False
_injected_provider = False
_runs_counter = None
_run_duration = None
_tool_counter = None


# ----------------------------------------------------------------------
# 初始化
# ----------------------------------------------------------------------


def _sampler():
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased

    ratio = max(0.0, min(1.0, settings.otel_sample_ratio))
    if ratio >= 1.0:
        return ParentBased(ALWAYS_ON)
    return ParentBased(TraceIdRatioBased(ratio))


def _build_otlp_span_exporter() -> SpanExporter | None:
    endpoint = settings.otel_exporter_otlp_endpoint.strip()
    if not endpoint:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:  # pragma: no cover - 依赖缺失时降级
        logger.warning("OTLP exporter not installed, spans will be dropped")
        return None
    return OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")


def _build_otlp_metric_reader() -> MetricReader | None:
    endpoint = settings.otel_exporter_otlp_endpoint.strip()
    if not endpoint:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    except ImportError:  # pragma: no cover - 依赖缺失时降级
        logger.warning("OTLP metric exporter not installed, metrics will be dropped")
        return None
    return PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint.rstrip('/')}/v1/metrics")
    )


def init_telemetry(
    *,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
    force: bool = False,
) -> bool:
    """初始化 provider。返回是否真的初始化了。

    `span_exporter` / `metric_reader` 是测试与私有部署的注入点；
    传了它们就无视 `OTEL_ENABLED`。
    """
    global _initialized, _injected_provider
    if _initialized and not force:
        return False

    injected = span_exporter is not None or metric_reader is not None
    if not settings.otel_enabled and not injected:
        return False

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": SERVICE_VERSION,
            "deployment.environment": settings.environment,
        }
    )

    provider = TracerProvider(resource=resource, sampler=_sampler())
    exporter = span_exporter or _build_otlp_span_exporter()
    if exporter is not None:
        # 注入的导出器用于测试，用同步处理器保证断言时数据已就绪
        processor = (
            SimpleSpanProcessor(exporter)
            if span_exporter is not None
            else BatchSpanProcessor(exporter)
        )
        provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    reader = metric_reader or _build_otlp_metric_reader()
    metrics.set_meter_provider(
        MeterProvider(resource=resource, metric_readers=[reader] if reader else [])
    )

    _initialized = True
    _injected_provider = injected
    logger.info(
        "telemetry initialized",
        extra={
            "fields": {
                "service": settings.otel_service_name,
                "environment": settings.environment,
                "exporting": exporter is not None,
            }
        },
    )
    return True


def shutdown_telemetry() -> None:
    """关闭 provider 并 flush。

    注入的 provider（测试、嵌入方）不归应用管：关掉共享实例会让同一进程里
    后续的 span 全部丢失——这正是多个 TestClient 各自触发 lifespan 时会发生的事。
    """
    if _injected_provider:
        return
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()


# ----------------------------------------------------------------------
# 链路
# ----------------------------------------------------------------------


def get_tracer():
    return trace.get_tracer(_TRACER_NAME)


@contextmanager
def span(name: str, /, **attributes: Any) -> Iterator[trace.Span]:
    """创建一个 span 并设为当前 span；属性值为 None 时跳过。"""
    with get_tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def current_trace_ids() -> tuple[str | None, str | None]:
    """给日志用的 trace_id / span_id；不在有效 span 内时返回 (None, None)。"""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return format_trace_id(context.trace_id), format_span_id(context.span_id)


# ----------------------------------------------------------------------
# 指标（刻意保持低基数）
# ----------------------------------------------------------------------


def _instruments():
    global _runs_counter, _run_duration, _tool_counter
    if _runs_counter is None:
        meter = metrics.get_meter(_TRACER_NAME)
        _runs_counter = meter.create_counter(
            "agent.runs", description="Agent 运行次数"
        )
        _run_duration = meter.create_histogram(
            "agent.run.duration_ms", unit="ms", description="Agent 运行耗时"
        )
        _tool_counter = meter.create_counter(
            "tool.invocations", description="工具调用次数（按状态）"
        )
    return _runs_counter, _run_duration, _tool_counter


def record_run(*, status: str, duration_ms: float) -> None:
    runs, duration, _ = _instruments()
    runs.add(1, {"status": status})
    duration.record(duration_ms, {"status": status})


def record_tool(*, status: str) -> None:
    _, _, tools = _instruments()
    # 不把工具名放进属性：工具数量会随租户增长，容易变成高基数
    tools.add(1, {"status": status})

"""结构化（JSON）日志。

输出单行 JSON，天然携带 request_id / run_id，便于后续接 OpenTelemetry / 日志采集。
同时接管 uvicorn 的 logger，避免同一请求出现一条 JSON 和一条纯文本重复记录。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.request_context import request_id_var, run_id_var


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染成一行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }

        run_id = run_id_var.get()
        if run_id:
            payload["run_id"] = run_id

        # 业务代码通过 extra={"fields": {...}} 附加上下文字段。
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """配置根 logger 与 uvicorn logger。重复调用是安全的。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # uvicorn 默认自带 handler 且 propagate=False，会绕过上面的 JSON 格式，这里统一接管。
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(level.upper())

    # 访问日志由 RequestContextMiddleware 自己结构化输出，关掉 uvicorn 的重复记录。
    access = logging.getLogger("uvicorn.access")
    access.handlers = [handler]
    access.propagate = False
    access.setLevel(logging.WARNING)


def log_event(logger: logging.Logger, message: str, /, *, level: int = logging.INFO, **fields: Any) -> None:
    """带结构化字段地记录一条日志。"""
    logger.log(level, message, extra={"fields": fields})

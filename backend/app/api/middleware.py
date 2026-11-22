"""请求上下文中间件：注入 request_id、输出结构化访问日志。"""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import Response

from app.api.error_handlers import error_response
from app.core.errors import ErrorCode
from app.core.request_context import request_id_var, run_id_var

logger = logging.getLogger(__name__)


async def request_context_middleware(request: Request, call_next) -> Response:
    request_id = request.headers.get("X-Request-ID") or f"req_{uuid4().hex}"
    request_token = request_id_var.set(request_id)
    run_token = run_id_var.set(None)
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception(
            "request failed",
            extra={
                "fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": 500,
                    "duration_ms": duration_ms,
                }
            },
        )
        response = error_response(500, ErrorCode.INTERNAL_ERROR, "服务器内部错误")
    else:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request",
        extra={
            "fields": {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            }
        },
    )

    run_id_var.reset(run_token)
    request_id_var.reset(request_token)
    return response

"""统一错误响应。

对外契约固定为：

```json
{"error": {"code": "not_found", "message": "Agent not found",
           "request_id": "req_...", "details": null}}
```

- `code` 是稳定的机器可读标识，客户端应按它分支；
- `message` 是可以安全展示的文案；
- `request_id` 与响应头 `X-Request-ID` 一致，便于对照日志排查；
- 未预期异常只返回 `internal_error`，细节仅进日志，避免信息泄露。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, ErrorCode
from app.core.request_context import request_id_var

logger = logging.getLogger(__name__)

_STATUS_TO_CODE = {
    400: ErrorCode.VALIDATION_ERROR,
    401: "unauthorized",
    403: "forbidden",
    404: ErrorCode.NOT_FOUND,
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: ErrorCode.VALIDATION_ERROR,
    429: "rate_limited",
    424: ErrorCode.MODEL_NOT_CONFIGURED,
    500: ErrorCode.INTERNAL_ERROR,
    502: ErrorCode.MODEL_PROVIDER_ERROR,
    504: ErrorCode.MODEL_PROVIDER_ERROR,
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = request_id_var.get()
    error: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details is not None:
        error["details"] = details
    merged = {"X-Request-ID": request_id, **(headers or {})}
    return JSONResponse(status_code=status_code, content={"error": error}, headers=merged)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    log = logger.warning if exc.status_code >= 500 else logger.info
    log(
        "request failed with AppError",
        extra={
            "fields": {
                "path": request.url.path,
                "status": exc.status_code,
                "code": exc.code,
            }
        },
    )
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _STATUS_TO_CODE.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else "请求失败"
    return error_response(exc.status_code, code, message)


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # exc.errors() 里可能带不可序列化的 ctx，这里只保留稳定字段。
    details = [
        {"loc": list(error.get("loc", ())), "msg": error.get("msg", ""), "type": error.get("type", "")}
        for error in exc.errors()
    ]
    return error_response(422, ErrorCode.VALIDATION_ERROR, "请求参数校验失败", details)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # 不把异常内容回显给客户端，只记录到日志。
    logger.exception(
        "unhandled exception",
        extra={"fields": {"path": request.url.path, "method": request.method}},
    )
    return error_response(500, ErrorCode.INTERNAL_ERROR, "服务器内部错误")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)

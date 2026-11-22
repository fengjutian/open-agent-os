"""请求上下文中间件：注入 request_id、输出结构化访问日志。

这里刻意用**纯 ASGI 中间件**而不是 `@app.middleware("http")`：
后者基于 `BaseHTTPMiddleware`，会把 `StreamingResponse` 的响应体缓冲起来，
导致 SSE 变成「攒够再一次吐出来」——流式就名存实亡了。
"""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.error_handlers import error_response
from app.core.errors import ErrorCode
from app.core.request_context import principal_key_id_var, request_id_var, run_id_var
from app.core.tenancy import reset_tenant, set_tenant

logger = logging.getLogger(__name__)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._header(scope, b"x-request-id") or f"req_{uuid4().hex}"
        request_token = request_id_var.set(request_id)
        run_token = run_id_var.set(None)
        principal_token = principal_key_id_var.set(None)
        # 每个请求从默认租户开始：即使上一次请求残留了上下文，也不会串租户。
        tenant_token = set_tenant(None)

        started = time.perf_counter()
        state: dict[str, object] = {"status": 500, "started": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["started"] = True
                state["status"] = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request failed",
                extra={
                    "fields": {
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": 500,
                        "duration_ms": duration_ms,
                    }
                },
            )
            if not state["started"]:
                # 还没开始写响应，才能改状态码。
                await error_response(500, ErrorCode.INTERNAL_ERROR, "服务器内部错误")(
                    scope, receive, send
                )
        else:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "request",
                extra={
                    "fields": {
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": state["status"],
                        "duration_ms": duration_ms,
                    }
                },
            )
        finally:
            principal_key_id_var.reset(principal_token)
            run_id_var.reset(run_token)
            reset_tenant(tenant_token)
            request_id_var.reset(request_token)

    @staticmethod
    def _header(scope: Scope, name: bytes) -> str | None:
        for key, value in scope.get("headers", []):
            if key == name:
                return value.decode("latin-1")
        return None

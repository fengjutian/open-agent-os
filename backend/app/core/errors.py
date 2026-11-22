"""业务错误类型与稳定的错误码。

原则：对外只暴露机器可读的 code 和安全的 message，不把上游原始错误体、
堆栈或配置细节回显给调用方；细节只进日志。
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    """对外契约中的错误码（客户端可按它分支，不要按 message 文案分支）。"""

    NOT_FOUND = "not_found"
    VALIDATION_ERROR = "validation_error"
    CONFLICT = "conflict"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    MODEL_NOT_CONFIGURED = "model_not_configured"
    MODEL_PROVIDER_ERROR = "model_provider_error"
    INTERNAL_ERROR = "internal_error"

    # ---- V0.2 工具层 ----
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_DISABLED = "tool_disabled"
    TOOL_ARGUMENTS_INVALID = "tool_arguments_invalid"
    POLICY_DENIED = "policy_denied"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    TOOL_TIMEOUT = "tool_timeout"
    EXECUTOR_NOT_AVAILABLE = "executor_not_available"


class AppError(Exception):
    """可安全返回给客户端业务错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

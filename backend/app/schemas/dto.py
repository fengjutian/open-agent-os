from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _assume_utc(value: object) -> object:
    """SQLite 不保存时区，读回来是 naive datetime，这里显式补上 UTC。

    否则接口会输出 "2026-11-22T12:20:06"（真实含义是 UTC），
    前端按本地时区解析就会整体偏 8 小时。
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


UtcDatetime = Annotated[datetime, BeforeValidator(_assume_utc)]


class Page[T](BaseModel):
    """列表接口的统一分页信封。"""

    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    details: Any | None = None


class ErrorEnvelope(BaseModel):
    """错误响应信封，用于 OpenAPI 文档声明。"""

    error: ErrorDetail


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    system_prompt: str = Field(default="You are a helpful assistant.", max_length=20000)
    # 省略时使用 settings.model_name，避免模型名在多处硬编码。
    model: str | None = Field(default=None, max_length=120)


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    system_prompt: str | None = Field(default=None, max_length=20000)
    model: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def reject_explicit_nulls(self) -> "AgentUpdate":
        """这几列在库中是 NOT NULL：允许"不传"（保持原值），但不允许显式传 null。

        否则 setattr 会把 None 写进 NOT NULL 列，触发 IntegrityError 的裸 500。
        description 列可空，显式传 null 用以清空，故不在此列。
        """
        for field in ("name", "system_prompt", "model"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} 不可为 null")
        return self


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str | None
    system_prompt: str
    model: str
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ConversationCreate(BaseModel):
    title: str = Field(default="New conversation", max_length=200)


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    agent_id: str
    title: str
    created_at: UtcDatetime
    updated_at: UtcDatetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=30000)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    conversation_id: str
    role: str
    content: str
    created_at: UtcDatetime


class SendMessageResponse(BaseModel):
    # run_id 用于串联一次 Agent 运行的所有日志/事件；V0.2 起会落库为 Run 表。
    run_id: str
    user_message: MessageRead
    assistant_message: MessageRead


# ---------------------------------------------------------------------------
# V0.2 工具层
# ---------------------------------------------------------------------------

TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_.-]{1,63}$"


def _empty_object_schema() -> dict[str, Any]:
    # 没声明参数的工具，默认不接受任何参数（安全默认）。
    return {"type": "object", "additionalProperties": False}


class ToolCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64, pattern=TOOL_NAME_PATTERN)
    description: str = Field(default="", max_length=2000)
    input_schema: dict[str, Any] = Field(default_factory=_empty_object_schema)
    permissions: list[str] = Field(default_factory=list, max_length=20)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    execution_type: Literal["builtin", "executor"] = "builtin"
    enabled: bool = True


class ToolUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    permissions: list[str] | None = Field(default=None, max_length=20)
    timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    enabled: bool | None = None


class ToolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    permissions: list[str]
    timeout_seconds: float
    execution_type: str
    enabled: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime


class PolicyRuleCreate(BaseModel):
    # 为空表示全局规则
    agent_id: str | None = None
    # 具体工具名，或 "*" 表示任意工具
    tool_name: str = Field(min_length=1, max_length=120)
    effect: Literal["allow", "deny"]
    priority: int = Field(default=100, ge=0, le=10000)
    note: str | None = Field(default=None, max_length=500)


class PolicyRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    agent_id: str | None
    tool_name: str
    effect: str
    priority: int
    enabled: bool
    note: str | None
    created_at: UtcDatetime


class ToolInvokeRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = None


class ToolInvokeResponse(BaseModel):
    run_id: str
    invocation_id: str
    tool_name: str
    status: str  # succeeded | denied | failed
    allowed: bool
    reason: str
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


class ToolInvocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    run_id: str
    agent_id: str | None
    conversation_id: str | None
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    status: str
    error_code: str | None
    error_message: str | None
    started_at: UtcDatetime
    finished_at: UtcDatetime | None


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    run_id: str | None
    request_id: str | None
    actor: str
    action: str
    resource: str
    decision: str | None
    reason: str | None
    detail: dict[str, Any] | None
    created_at: UtcDatetime


class AgentToolRead(BaseModel):
    """某个 Agent 视角下的工具可用性（注册状态 + 策略裁决）。"""

    name: str
    description: str
    execution_type: str
    enabled: bool
    allowed: bool
    reason: str

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
    # 引用的技能名，必须已存在
    skill_names: list[str] = Field(default_factory=list, max_length=20)


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    system_prompt: str | None = Field(default=None, max_length=20000)
    model: str | None = Field(default=None, max_length=120)
    skill_names: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def reject_explicit_nulls(self) -> "AgentUpdate":
        """这几列在库中是 NOT NULL：允许"不传"（保持原值），但不允许显式传 null。

        否则 setattr 会把 None 写进 NOT NULL 列，触发 IntegrityError 的裸 500。
        description 列可空，显式传 null 用以清空，故不在此列。
        """
        for field in ("name", "system_prompt", "model", "skill_names"):
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
    skill_names: list[str]
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ConversationCreate(BaseModel):
    title: str = Field(default="New conversation", max_length=200)
    # ask / plan / craft；缺省用服务端默认值（craft）
    mode: str | None = Field(default=None, pattern="^(ask|plan|craft)$")


class ConversationUpdate(BaseModel):
    """PATCH 语义：只改发过来的字段；显式 null 一律拒绝。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    mode: str | None = Field(default=None, pattern="^(ask|plan|craft)$")

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        for name in ("title", "mode"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} 不能显式设为 null")
        return self


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    agent_id: str
    title: str
    mode: str
    created_at: UtcDatetime
    updated_at: UtcDatetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=30000)
    # 本轮的临时模式覆盖，不改变会话上的粘性设置
    mode: str | None = Field(default=None, pattern="^(ask|plan|craft)$")


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
    execution_type: Literal["builtin", "mcp", "executor"] = "builtin"
    # execution_type=mcp 时形如 {"server": "echo", "tool": "echo"}；
    # execution_type=executor 时形如 {"runtime": "shell", "image": "python:3.12-slim"}，
    # 命令必须来自调用参数（config 里不允许携带命令）。
    config: dict[str, Any] | None = None
    enabled: bool = True
    # 声明该工具没有副作用；ask / plan 模式下只有只读工具对模型可见
    read_only: bool = False


class ToolUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    permissions: list[str] | None = Field(default=None, max_length=20)
    timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    enabled: bool | None = None
    read_only: bool | None = None


class ToolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    permissions: list[str]
    timeout_seconds: float
    execution_type: str
    config: dict[str, Any] | None = None
    enabled: bool
    read_only: bool = False
    created_at: UtcDatetime
    updated_at: UtcDatetime


class McpSyncResult(BaseModel):
    """同步 MCP 服务器的结果：注册成功的工具与跳过的原因。"""

    server: str
    registered: list[ToolRead] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


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


class ToolArtifactRead(BaseModel):
    """隔离执行器产物索引。文件内容通过下载接口获取。"""

    model_config = ConfigDict(from_attributes=True)
    id: str
    invocation_id: str
    run_id: str
    tool_name: str
    path: str
    size_bytes: int
    sha256: str
    created_at: UtcDatetime


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
    # 只有 executor 工具会产生；按路径排序
    artifacts: list[ToolArtifactRead] = Field(default_factory=list)


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


class CronJobCreate(BaseModel):
    name: str = Field(default="scheduled job", min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=30000)
    # 标准 5 段 cron（分 时 日 月 周），UTC；也支持 @daily 等别名
    schedule: str = Field(min_length=1, max_length=120)
    conversation_id: str | None = None
    enabled: bool = True


class CronJobUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=30000)
    schedule: str | None = Field(default=None, min_length=1, max_length=120)
    conversation_id: str | None = None
    enabled: bool | None = None


class CronJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    agent_id: str
    conversation_id: str | None = None
    name: str
    prompt: str
    schedule: str
    enabled: bool
    next_run_at: UtcDatetime
    last_run_at: UtcDatetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime


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


class AuditVerifyResult(BaseModel):
    """哈希链校验结果。"""

    valid: bool
    # 已校验的链接行数
    checked: int
    # 引入哈希链之前写入的历史行（未链接，不影响结论）
    legacy: int = 0
    # 首个断裂点的 seq（如果有）
    broken_at: int | None = None
    reason: str | None = None


class AgentToolRead(BaseModel):
    """某个 Agent 视角下的工具可用性（注册状态 + 策略裁决）。"""

    name: str
    description: str
    execution_type: str
    enabled: bool
    allowed: bool
    reason: str


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: Literal["admin", "developer", "viewer"] = "viewer"
    # 仅在为**其他租户**发密钥时需要显式指定；默认落在调用者所在租户
    tenant_id: str | None = Field(default=None, max_length=36)


class ApiKeyUpdate(BaseModel):
    enabled: bool | None = None


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    key_prefix: str
    role: str
    enabled: bool
    created_at: UtcDatetime
    last_used_at: UtcDatetime | None


class ApiKeyCreated(ApiKeyRead):
    """创建响应：明文密钥只在这里出现一次。"""

    api_key: str


class MeRead(BaseModel):
    """当前身份，供前端决定显示哪些操作（真正的校验仍在服务端）。"""

    key_id: str
    name: str
    role: str
    permissions: list[str]
    # api_key / oidc / none
    auth_method: str = "api_key"


# ---------------------------------------------------------------------------
# Run / RunEvent
# ---------------------------------------------------------------------------


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    agent_id: str | None
    conversation_id: str | None
    key_id: str | None
    status: str
    model: str | None
    iterations: int
    tool_calls: int
    prompt_chars: int
    answer_chars: int
    error_code: str | None
    error_message: str | None
    started_at: UtcDatetime
    finished_at: UtcDatetime | None


class RunEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    run_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: UtcDatetime


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


class SkillCreate(BaseModel):
    # 复用工具名的字符集约束，保持命名一致
    name: str = Field(min_length=2, max_length=64, pattern=TOOL_NAME_PATTERN)
    description: str = Field(default="", max_length=2000)
    instructions: str = Field(min_length=1, max_length=20000)
    enabled: bool = True


class SkillUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    instructions: str | None = Field(default=None, min_length=1, max_length=20000)
    enabled: bool | None = None


class SkillRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str
    instructions: str
    enabled: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime


# ---------------------------------------------------------------------------
# 租户与成员
# ---------------------------------------------------------------------------

TENANT_ID_PATTERN = r"^[a-z][a-z0-9-]{1,35}$"


class TenantCreate(BaseModel):
    id: str = Field(min_length=2, max_length=36, pattern=TENANT_ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    status: str
    created_at: UtcDatetime


class MembershipCreate(BaseModel):
    # 与 Principal.key_id 对齐，例如 oidc:user-1
    subject: str = Field(min_length=1, max_length=200)
    role: Literal["admin", "developer", "viewer"] = "viewer"


class MembershipUpdate(BaseModel):
    role: Literal["admin", "developer", "viewer"]


class MembershipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    subject: str
    role: str
    created_at: UtcDatetime

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.tenancy import current_tenant
from app.db.session import Base


def now_utc():
    return datetime.now(UTC)


class TenantScoped(Base):
    """所有带租户的模型的抽象基类。

    用**抽象映射基类**而不是 mixin，是为了让 `with_loader_criteria` 精确地只作用于
    「有租户的实体」。如果把 `Base` 传进去，Tenant 这类全局表也会被纳入，而条件函数
    返回 None 时 SQLAlchemy 生成的是 `WHERE NULL`——那会把表整个滤空。
    """

    __abstract__ = True

    tenant_id: Mapped[str] = mapped_column(
        String(36), default=current_tenant, nullable=False, index=True
    )

class Agent(TenantScoped):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str] = mapped_column(Text, default="You are a helpful assistant.")
    model: Mapped[str] = mapped_column(String(120), default="gpt-4o-mini")
    # 引用的 Skill 名称列表。刻意用名称而不是外键：
    # 运行时按名称解析、缺失或停用就跳过，避免改了技能就把历史 Agent 卡住。
    # 代价是重命名技能会断引用，因此创建/更新时会校验名称必须存在。
    skill_names: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )

class Conversation(TenantScoped):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
    agent: Mapped[Agent] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at"
    )

class Message(TenantScoped):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Run(TenantScoped):
    """一次 Agent 运行的主体记录。

    run_id 就是这张表的主键；此前它只存在于日志里，现在可以按 id 回溯整条链路。
    """

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 发起这次运行的 API Key，用于配额统计与追责
    key_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    prompt_chars: Mapped[int] = mapped_column(Integer, default=0)
    answer_chars: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunEvent(TenantScoped):
    """运行过程中的事件流（追加写，不修改）。

    不建到 runs 的外键：与审计同一思路，事件记录的寿命不依赖业务数据的生命周期。
    """

    __tablename__ = "run_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Skill(TenantScoped):
    """可复用的指令包。

    一个 Skill 就是一段被命名的「行为约定」，可以被多个 Agent 引用。
    注入点只有一处：拼上下文时并入 system prompt，因此同样受上下文预算约束。
    """

    __tablename__ = "skills"
    # 名称在租户内唯一：不同租户可以各自拥有同名技能
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_skills_tenant_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class Tenant(Base):
    """组织（租户）。

    它本身就是隔离边界，因此**不**继承 TenantMixin——它没有上级租户。
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # active / disabled：停用后该租户的所有请求都会被拒
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class Membership(TenantScoped):
    """成员关系：把某个身份（OIDC subject）绑到租户内的角色。

    有了它，角色就不必完全信任 IdP 的 claim——平台可以按租户覆盖。
    API Key 本身带角色，不需要 membership。
    """

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "subject", name="uq_memberships_tenant_subject"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    # 与 Principal.key_id 对齐，例如 oidc:user-1
    subject: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class ApiKey(TenantScoped):
    """API 密钥。

    只保存 SHA-256 摘要，明文仅在创建/引导时返回或打印一次。
    key_prefix 用于在列表页里辨认是哪一把，不参与鉴权。
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    # 密钥名保持全局唯一（人类标签，管理面小）；真正区分租户的是 tenant_id
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # 哈希全局唯一：一把密钥只属于一个租户
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolDefinition(TenantScoped):
    """工具注册表条目。

    模型只能「看到」注册在这里的工具；未注册的工具名一律不存在。
    """

    __tablename__ = "tool_definitions"
    # 名称在租户内唯一
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_tool_definitions_tenant_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # 入参的 JSON Schema；调用前必须通过它的校验。
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    # 该工具声明的权限标记，供策略与审计使用（例如 "read:time"）。
    permissions: Mapped[list] = mapped_column(JSON, default=list)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=10.0)
    # builtin：平台内建的确定性实现；mcp：转发给已配置的 MCP 服务器；
    # executor：（V0.3）交给隔离执行器，当前一律拒绝。
    execution_type: Mapped[str] = mapped_column(String(20), default="builtin")
    # execution_type=mcp 时形如 {"server": "echo", "tool": "echo"}。
    # 只允许引用 settings.mcp_servers 里声明过的服务器名，不允许携带命令行。
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class PolicyRule(TenantScoped):
    """服务端策略规则。

    agent_id 为空表示全局规则；priority 越大越优先；同优先级下 deny 胜过 allow。
    没有任何 allow 命中时默认拒绝（deny-by-default）。
    """

    __tablename__ = "policy_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # 具体工具名，或 "*" 表示任意工具。
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    effect: Mapped[str] = mapped_column(String(10), nullable=False)  # allow | deny
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ToolInvocation(TenantScoped):
    """一次工具调用的持久记录（无论成功、被拒还是失败）。

    故意不建到 agents 的外键：审计记录不应随 Agent 删除而消失。
    """

    __tablename__ = "tool_invocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # succeeded | denied | failed
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(TenantScoped):
    """审计事件。同样不挂外键，保证记录不可被级联删除。

    只追加：`seq` / `prev_hash` / `entry_hash` 构成按租户的哈希链，
    任何改写、删除或插入都会在 `verify_chain` 时暴露。
    """

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    # 租户内单调递增序号；历史行可能为 NULL（引入哈希链之前写入的）
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource: Mapped[str] = mapped_column(String(200), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(10), nullable=True)  # allow | deny | None
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)

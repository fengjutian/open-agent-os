from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def now_utc():
    return datetime.now(UTC)

class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str] = mapped_column(Text, default="You are a helpful assistant.")
    model: Mapped[str] = mapped_column(String(120), default="gpt-4o-mini")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )

class Conversation(Base):
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

class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class ToolDefinition(Base):
    """工具注册表条目。

    模型只能「看到」注册在这里的工具；未注册的工具名一律不存在。
    """

    __tablename__ = "tool_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # 入参的 JSON Schema；调用前必须通过它的校验。
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    # 该工具声明的权限标记，供策略与审计使用（例如 "read:time"）。
    permissions: Mapped[list] = mapped_column(JSON, default=list)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=10.0)
    # builtin：平台内建的确定性实现；executor：（V0.3）交给隔离执行器，当前一律拒绝。
    execution_type: Mapped[str] = mapped_column(String(20), default="builtin")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class PolicyRule(Base):
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


class ToolInvocation(Base):
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


class AuditLog(Base):
    """审计事件。同样不挂外键，保证记录不可被级联删除。"""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource: Mapped[str] = mapped_column(String(200), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(10), nullable=True)  # allow | deny | None
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)

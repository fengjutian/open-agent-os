from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, model_validator

class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    system_prompt: str = "You are a helpful assistant."
    model: str = "gpt-4o-mini"

class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    system_prompt: str | None = None
    model: str | None = None

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
    created_at: datetime
    updated_at: datetime

class ConversationCreate(BaseModel):
    title: str = Field(default="New conversation", max_length=200)

class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    agent_id: str
    title: str
    created_at: datetime
    updated_at: datetime

class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=30000)

class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    conversation_id: str
    role: str
    content: str
    created_at: datetime

class SendMessageResponse(BaseModel):
    user_message: MessageRead
    assistant_message: MessageRead

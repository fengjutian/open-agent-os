from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

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

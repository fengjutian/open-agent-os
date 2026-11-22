from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.entities import Agent, Conversation, Message
from app.services.model_gateway import get_model_provider

class AgentRuntime:
    async def respond(self, db: Session, conversation: Conversation, user_content: str):
        agent = db.get(Agent, conversation.agent_id)
        if agent is None:
            raise ValueError("Agent not found")
        user_message = Message(conversation_id=conversation.id, role="user", content=user_content)
        db.add(user_message)
        db.flush()
        history = db.scalars(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.asc())
        ).all()
        messages = [{"role": "system", "content": agent.system_prompt}]
        messages.extend({"role": m.role, "content": m.content} for m in history if m.role in {"user", "assistant"})
        answer = await get_model_provider().generate(model=agent.model, messages=messages)
        assistant_message = Message(conversation_id=conversation.id, role="assistant", content=answer)
        db.add(assistant_message)
        db.commit()
        db.refresh(user_message)
        db.refresh(assistant_message)
        return user_message, assistant_message

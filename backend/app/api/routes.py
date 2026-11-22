from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.entities import Agent, Conversation, Message
from app.schemas.dto import AgentCreate, AgentRead, AgentUpdate, ConversationCreate, ConversationRead, MessageCreate, MessageRead, SendMessageResponse
from app.services.agent_runtime import AgentRuntime

router = APIRouter(prefix="/api/v1")

@router.post("/agents", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
def create_agent(payload: AgentCreate, db: Session = Depends(get_db)):
    item = Agent(**payload.model_dump())
    db.add(item); db.commit(); db.refresh(item)
    return item

@router.get("/agents", response_model=list[AgentRead])
def list_agents(db: Session = Depends(get_db)):
    return db.scalars(select(Agent).order_by(Agent.created_at.desc())).all()

@router.get("/agents/{agent_id}", response_model=AgentRead)
def get_agent(agent_id: str, db: Session = Depends(get_db)):
    item = db.get(Agent, agent_id)
    if not item: raise HTTPException(404, "Agent not found")
    return item

@router.patch("/agents/{agent_id}", response_model=AgentRead)
def update_agent(agent_id: str, payload: AgentUpdate, db: Session = Depends(get_db)):
    item = db.get(Agent, agent_id)
    if not item: raise HTTPException(404, "Agent not found")
    for key, value in payload.model_dump(exclude_unset=True).items(): setattr(item, key, value)
    db.commit(); db.refresh(item)
    return item

@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: str, db: Session = Depends(get_db)):
    item = db.get(Agent, agent_id)
    if not item: raise HTTPException(404, "Agent not found")
    db.delete(item); db.commit()

@router.post("/agents/{agent_id}/conversations", response_model=ConversationRead, status_code=201)
def create_conversation(agent_id: str, payload: ConversationCreate, db: Session = Depends(get_db)):
    if not db.get(Agent, agent_id): raise HTTPException(404, "Agent not found")
    item = Conversation(agent_id=agent_id, title=payload.title)
    db.add(item); db.commit(); db.refresh(item)
    return item

@router.get("/agents/{agent_id}/conversations", response_model=list[ConversationRead])
def list_conversations(agent_id: str, db: Session = Depends(get_db)):
    if not db.get(Agent, agent_id): raise HTTPException(404, "Agent not found")
    return db.scalars(select(Conversation).where(Conversation.agent_id == agent_id).order_by(Conversation.updated_at.desc())).all()

@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
def list_messages(conversation_id: str, db: Session = Depends(get_db)):
    if not db.get(Conversation, conversation_id): raise HTTPException(404, "Conversation not found")
    return db.scalars(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at.asc())).all()

@router.post("/conversations/{conversation_id}/messages", response_model=SendMessageResponse)
async def send_message(conversation_id: str, payload: MessageCreate, db: Session = Depends(get_db)):
    conversation = db.get(Conversation, conversation_id)
    if not conversation: raise HTTPException(404, "Conversation not found")
    try:
        user_msg, assistant_msg = await AgentRuntime().respond(db, conversation, payload.content)
    except Exception as exc:
        db.rollback()
        raise HTTPException(502, f"Agent execution failed: {exc}") from exc
    return {"user_message": user_msg, "assistant_message": assistant_msg}

export interface Agent { id:string; name:string; description:string|null; system_prompt:string; model:string; created_at:string; updated_at:string }
export interface Conversation { id:string; agent_id:string; title:string; created_at:string; updated_at:string }
export interface Message { id:string; conversation_id:string; role:"system"|"user"|"assistant"; content:string; created_at:string }

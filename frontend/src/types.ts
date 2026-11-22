export interface Agent { id:string; name:string; description:string|null; system_prompt:string; model:string; created_at:string; updated_at:string }
export interface Conversation { id:string; agent_id:string; title:string; created_at:string; updated_at:string }
export interface Message { id:string; conversation_id:string; role:"system"|"user"|"assistant"; content:string; created_at:string }

/** 列表接口的统一分页信封。 */
export interface Page<T> { items:T[]; total:number; limit:number; offset:number }

/** 错误响应信封：code 稳定可分支，request_id 用于对照后端日志。 */
export interface ApiErrorBody { error:{ code:string; message:string; request_id?:string; details?:unknown } }

export interface SendMessageResponse { run_id:string; user_message:Message; assistant_message:Message }

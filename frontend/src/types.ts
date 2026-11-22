export interface Agent { id:string; name:string; description:string|null; system_prompt:string; model:string; created_at:string; updated_at:string }
export interface Conversation { id:string; agent_id:string; title:string; created_at:string; updated_at:string }
export interface Message { id:string; conversation_id:string; role:"system"|"user"|"assistant"; content:string; created_at:string }

/** 列表接口的统一分页信封。 */
export interface Page<T> { items:T[]; total:number; limit:number; offset:number }

/** 错误响应信封：code 稳定可分支，request_id 用于对照后端日志。 */
export interface ApiErrorBody { error:{ code:string; message:string; request_id?:string; details?:unknown } }

export interface SendMessageResponse { run_id:string; user_message:Message; assistant_message:Message }

/** 当前身份：用于决定界面显示哪些操作（真正的校验仍在服务端）。 */
export interface Me { key_id:string; name:string; role:string; permissions:string[] }

export interface Tool {
 id:string; name:string; description:string;
 input_schema:Record<string,unknown>;
 permissions:string[]; timeout_seconds:number;
 execution_type:"builtin"|"executor"|string; enabled:boolean;
 created_at:string; updated_at:string;
}

/** 某个 Agent 视角下的工具可用性。 */
export interface AgentTool {
 name:string; description:string; execution_type:string;
 enabled:boolean; allowed:boolean; reason:string;
}

export interface PolicyRule {
 id:string; agent_id:string|null; tool_name:string;
 effect:"allow"|"deny"|string; priority:number; enabled:boolean;
 note:string|null; created_at:string;
}

export interface AuditLog {
 id:string; run_id:string|null; request_id:string|null;
 actor:string; action:string; resource:string;
 decision:string|null; reason:string|null;
 detail:Record<string,unknown>|null; created_at:string;
}

export interface Run {
 id:string; agent_id:string|null; conversation_id:string|null; key_id:string|null;
 status:"running"|"succeeded"|"failed"|string;
 model:string|null; iterations:number; tool_calls:number;
 prompt_chars:number; answer_chars:number;
 error_code:string|null; error_message:string|null;
 started_at:string; finished_at:string|null;
}

export interface RunEvent {
 id:string; run_id:string; seq:number; type:string;
 payload:Record<string,unknown>; created_at:string;
}

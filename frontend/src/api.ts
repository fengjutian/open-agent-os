import type {Agent,AgentTool,AuditLog,Conversation,Me,Membership,Message,Page,PolicyRule,Run,RunEvent,SendMessageResponse,Tenant,Tool} from "./types";

const BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";
const KEY_STORAGE = "agentPlatformApiKey";

/** 带 HTTP 状态码的错误，便于区分 401（需要重新输入密钥）与其他失败。 */
export class ApiError extends Error {
 readonly status:number;
 constructor(message:string,status:number){
  super(message);
  this.name="ApiError";
  this.status=status;
 }
}

/**
 * API Key 只保存在浏览器本地，不进构建产物。
 * 生产环境若要更强的方案，应改成后端签发的短期会话令牌。
 */
export const keyStore={
 get():string{return localStorage.getItem(KEY_STORAGE) ?? "";},
 set(value:string):void{
  const trimmed=value.trim();
  if(trimmed) localStorage.setItem(KEY_STORAGE,trimmed); else localStorage.removeItem(KEY_STORAGE);
 },
};

/** 把后端的统一错误信封翻译成可展示的文案，附上 request_id 便于排查。 */
async function describeError(res:Response):Promise<string>{
 const text = await res.text();
 try{
  const body = JSON.parse(text);
  const err = body?.error;
  if(err?.message) return err.request_id ? `${err.message}（${err.request_id}）` : err.message;
 }catch{ /* 非 JSON，回退到原文 */ }
 return text || `HTTP ${res.status}`;
}

async function request<T>(path:string, init?:RequestInit):Promise<T>{
 const key = keyStore.get();
 const res = await fetch(`${BASE}${path}`,{
  ...init,
  headers:{
   "Content-Type":"application/json",
   ...(key ? {Authorization:`Bearer ${key}`} : {}),
   ...init?.headers,
  },
 });
 if(!res.ok) throw new ApiError(await describeError(res), res.status);
 if(res.status===204) return undefined as T;
 return res.json() as Promise<T>;
}

export const api={
 agents:()=>request<Page<Agent>>("/agents"), createAgent:(data:Pick<Agent,"name"|"description"|"system_prompt"|"model">)=>request<Agent>("/agents",{method:"POST",body:JSON.stringify(data)}),
 conversations:(id:string)=>request<Page<Conversation>>(`/agents/${id}/conversations`),
 createConversation:(id:string)=>request<Conversation>(`/agents/${id}/conversations`,{method:"POST",body:JSON.stringify({title:"New conversation"})}),
 messages:(id:string)=>request<Page<Message>>(`/conversations/${id}/messages`),
 sendMessage:(id:string,content:string)=>request<SendMessageResponse>(`/conversations/${id}/messages`,{method:"POST",body:JSON.stringify({content})}),

 me:()=>request<Me>("/me"),

 tools:()=>request<Page<Tool>>("/tools"),
 createTool:(data:{name:string;description?:string;input_schema?:Record<string,unknown>;execution_type?:"builtin"|"mcp"|"executor";config?:Record<string,unknown>})=>
  request<Tool>("/tools",{method:"POST",body:JSON.stringify(data)}),
 updateTool:(id:string,patch:{enabled?:boolean;description?:string})=>
  request<Tool>(`/tools/${id}`,{method:"PATCH",body:JSON.stringify(patch)}),
 agentTools:(agentId:string)=>request<Page<AgentTool>>(`/agents/${agentId}/tools`),

 policyRules:(agentId:string)=>request<Page<PolicyRule>>(`/policy-rules?agent_id=${encodeURIComponent(agentId)}`),
 createPolicyRule:(data:{agent_id:string|null;tool_name:string;effect:"allow"|"deny";priority?:number})=>
  request<PolicyRule>("/policy-rules",{method:"POST",body:JSON.stringify(data)}),

 auditLogs:(limit=20)=>request<Page<AuditLog>>(`/audit-logs?limit=${limit}`),

 runs:(agentId?:string,limit=20)=>
  request<Page<Run>>(`/runs?limit=${limit}${agentId?`&agent_id=${encodeURIComponent(agentId)}`:""}`),
 runEvents:(runId:string)=>request<Page<RunEvent>>(`/runs/${runId}/events?limit=100`),

 currentTenant:()=>request<Tenant>("/tenants/current"),
 createTenant:(data:{id:string;name:string})=>
  request<Tenant>("/tenants",{method:"POST",body:JSON.stringify(data)}),
 memberships:(limit=100)=>request<Page<Membership>>(`/memberships?limit=${limit}`),
 addMembership:(data:{subject:string;role:string})=>
  request<Membership>("/memberships",{method:"POST",body:JSON.stringify(data)}),
 updateMembership:(id:string,role:string)=>
  request<Membership>(`/memberships/${id}`,{method:"PATCH",body:JSON.stringify({role})}),
 removeMembership:(id:string)=>request<void>(`/memberships/${id}`,{method:"DELETE"}),
};

/** 流式回调：服务端按 SSE 事件推送。 */
export interface StreamHandlers {
 onStarted?(runId:string):void;
 onDelta?(text:string):void;
 onToolCall?(tool:string):void;
 onToolResult?(tool:string,status:string):void;
 onCompleted?(payload:{run_id:string;user_message:Message;assistant_message:Message}):void;
 onEnd?(status:string,reason?:string):void;
 onError?(code:string,message:string):void;
}

function handleSseChunk(chunk:string,handlers:StreamHandlers){
 let event="message";
 let data="";
 for(const line of chunk.split("\n")){
  if(line.startsWith("event:")) event=line.slice(6).trim();
  else if(line.startsWith("data:")) data+=line.slice(5).trim();
 }
 if(!data) return;

 let payload:Record<string,unknown>;
 try{payload=JSON.parse(data) as Record<string,unknown>;}catch{return;}

 if(event==="run.started") handlers.onStarted?.(String(payload.run_id ?? ""));
 else if(event==="delta") handlers.onDelta?.(String(payload.text ?? ""));
 else if(event==="tool.call") handlers.onToolCall?.(String(payload.tool ?? ""));
 else if(event==="tool.result") handlers.onToolResult?.(String(payload.tool ?? ""),String(payload.status ?? ""));
 else if(event==="completed") handlers.onCompleted?.(payload as never);
 else if(event==="stream.end") handlers.onEnd?.(String(payload.status ?? ""),payload.reason ? String(payload.reason) : undefined);
 else if(event==="error") handlers.onError?.(String(payload.code ?? "error"),String(payload.message ?? ""));
}

async function consumeSse(body:ReadableStream<Uint8Array>,handlers:StreamHandlers):Promise<void>{
 const reader = body.getReader();
 const decoder = new TextDecoder();
 let buffer = "";
 while(true){
  const {done,value} = await reader.read();
  if(done) break;
  buffer += decoder.decode(value,{stream:true});
  let boundary = buffer.indexOf("\n\n");
  while(boundary >= 0){
   handleSseChunk(buffer.slice(0,boundary),handlers);
   buffer = buffer.slice(boundary+2);
   boundary = buffer.indexOf("\n\n");
  }
 }
 if(buffer.trim()) handleSseChunk(buffer,handlers);
}

/**
 * 以 SSE 流式发送消息。
 * 注意：这里不能用 EventSource（它只支持 GET 且不能带 Authorization 头），
 * 因此用 fetch + ReadableStream 自己解析。
 */
export async function sendMessageStream(conversationId:string,content:string,handlers:StreamHandlers):Promise<void>{
 const key = keyStore.get();
 const res = await fetch(`${BASE}/conversations/${conversationId}/messages/stream`,{
  method:"POST",
  headers:{"Content-Type":"application/json",...(key ? {Authorization:`Bearer ${key}`} : {})},
  body:JSON.stringify({content}),
 });
 if(!res.ok) throw new ApiError(await describeError(res), res.status);
 if(!res.body) throw new ApiError("当前环境不支持流式响应",0);
 await consumeSse(res.body,handlers);
}

/**
 * 断线续传：按游标回放某次运行的事件。
 * 服务端会一直跟随到运行结束（或超时），因此这里可能等待较久。
 */
export async function resumeRunStream(runId:string,handlers:StreamHandlers,after=0):Promise<void>{
 const key = keyStore.get();
 const res = await fetch(`${BASE}/runs/${runId}/events/stream?after=${after}`,{
  headers:{...(key ? {Authorization:`Bearer ${key}`} : {})},
 });
 if(!res.ok) throw new ApiError(await describeError(res), res.status);
 if(!res.body) throw new ApiError("当前环境不支持流式响应",0);
 await consumeSse(res.body,handlers);
}

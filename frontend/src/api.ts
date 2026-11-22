import type {Agent,Conversation,Message,Page,SendMessageResponse} from "./types";
const BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";

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
 const res = await fetch(`${BASE}${path}`,{...init,headers:{"Content-Type":"application/json",...init?.headers}});
 if(!res.ok) throw new Error(await describeError(res));
 if(res.status===204) return undefined as T;
 return res.json() as Promise<T>;
}

export const api={
 agents:()=>request<Page<Agent>>("/agents"),
 createAgent:(data:Pick<Agent,"name"|"description"|"system_prompt"|"model">)=>request<Agent>("/agents",{method:"POST",body:JSON.stringify(data)}),
 conversations:(id:string)=>request<Page<Conversation>>(`/agents/${id}/conversations`),
 createConversation:(id:string)=>request<Conversation>(`/agents/${id}/conversations`,{method:"POST",body:JSON.stringify({title:"New conversation"})}),
 messages:(id:string)=>request<Page<Message>>(`/conversations/${id}/messages`),
 sendMessage:(id:string,content:string)=>request<SendMessageResponse>(`/conversations/${id}/messages`,{method:"POST",body:JSON.stringify({content})})
};

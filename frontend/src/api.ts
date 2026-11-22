import type {Agent,Conversation,Message} from "./types";
const BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";
async function request<T>(path:string, init?:RequestInit):Promise<T>{
 const res=await fetch(`${BASE}${path}`,{...init,headers:{"Content-Type":"application/json",...init?.headers}});
 if(!res.ok) throw new Error(await res.text() || `HTTP ${res.status}`);
 if(res.status===204) return undefined as T;
 return res.json() as Promise<T>;
}
export const api={
 agents:()=>request<Agent[]>("/agents"),
 createAgent:(data:Pick<Agent,"name"|"description"|"system_prompt"|"model">)=>request<Agent>("/agents",{method:"POST",body:JSON.stringify(data)}),
 conversations:(id:string)=>request<Conversation[]>(`/agents/${id}/conversations`),
 createConversation:(id:string)=>request<Conversation>(`/agents/${id}/conversations`,{method:"POST",body:JSON.stringify({title:"New conversation"})}),
 messages:(id:string)=>request<Message[]>(`/conversations/${id}/messages`),
 sendMessage:(id:string,content:string)=>request<{user_message:Message;assistant_message:Message}>(`/conversations/${id}/messages`,{method:"POST",body:JSON.stringify({content})})
};

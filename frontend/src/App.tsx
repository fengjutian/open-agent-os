import {useEffect,useState} from "react";
import {api} from "./api";
import type {Agent,Conversation,Message} from "./types";

export default function App(){
 const [agents,setAgents]=useState<Agent[]>([]),[agent,setAgent]=useState<Agent|null>(null);
 const [conversations,setConversations]=useState<Conversation[]>([]),[conversation,setConversation]=useState<Conversation|null>(null);
 const [messages,setMessages]=useState<Message[]>([]),[input,setInput]=useState(""),[busy,setBusy]=useState(false),[error,setError]=useState("");
 async function refresh(){const data=await api.agents();setAgents(data);if(!agent&&data.length)setAgent(data[0]);}
 useEffect(()=>{refresh().catch(e=>setError(String(e)));},[]);
 useEffect(()=>{if(!agent)return;setConversation(null);setMessages([]);api.conversations(agent.id).then(setConversations).catch(e=>setError(String(e)));},[agent?.id]);
 async function open(c:Conversation){setConversation(c);try{setMessages(await api.messages(c.id));}catch(e){setError(String(e));}}
 async function addAgent(){const name=prompt("Agent 名称");if(!name?.trim())return;try{const a=await api.createAgent({name:name.trim(),description:"AI assistant",system_prompt:"You are a helpful assistant. Answer in Chinese unless asked otherwise.",model:"gpt-4o-mini"});const data=await api.agents();setAgents(data);setAgent(a);}catch(e){setError(String(e));}}
 async function addConversation(){if(!agent)return;try{const c=await api.createConversation(agent.id);setConversations(old=>[c,...old]);await open(c);}catch(e){setError(String(e));}}
 async function send(){if(!conversation||!input.trim()||busy)return;const text=input.trim();setInput("");setBusy(true);setError("");try{const r=await api.sendMessage(conversation.id,text);setMessages(old=>[...old,r.user_message,r.assistant_message]);}catch(e){setError(String(e));setInput(text);}finally{setBusy(false);}}
 return <div className="shell"><aside className="sidebar"><div className="brand"><b>A</b><span><strong>Agent Platform</strong><small>Enterprise Workspace</small></span></div><div className="label">AGENTS <button onClick={addAgent}>＋</button></div>{agents.map(a=><button key={a.id} className={`agent ${agent?.id===a.id?"active":""}`} onClick={()=>setAgent(a)}><i>{a.name[0]?.toUpperCase()}</i><span><strong>{a.name}</strong><small>{a.model}</small></span></button>)}{!agents.length&&<p className="muted">点击 + 创建第一个 Agent</p>}<footer>● Local development</footer></aside>
 <main><header><div><small>WORKSPACE / AGENTS</small><h1>{agent?.name??"欢迎使用 Agent Platform"}</h1></div>{agent&&<button className="primary" onClick={addConversation}>新建会话</button>}</header>
 <div className="layout"><section className="sessions"><h3>会话</h3><p className="muted">选择会话开始协作</p>{conversations.map(c=><button key={c.id} className={`session ${conversation?.id===c.id?"active":""}`} onClick={()=>open(c)}>◷　{c.title}</button>)}</section>
 <section className="chat">{!conversation?<div className="welcome"><div>✳</div><h2>让 Agent 开始工作</h2><p>创建或选择会话，然后输入第一个任务。</p>{agent&&<button className="primary" onClick={addConversation}>创建会话</button>}</div>:<><div className="chat-head"><div><b>{conversation.title}</b><small>{agent?.name} · {agent?.model}</small></div><span>● Ready</span></div><div className="messages">{messages.map(m=><div key={m.id} className={`message ${m.role}`}><label>{m.role==="user"?"YOU":"AGENT"}</label><p>{m.content}</p></div>)}{busy&&<p className="muted">Agent 正在思考…</p>}</div><div className="composer"><textarea value={input} onChange={e=>setInput(e.target.value)} onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send();}}} placeholder="描述你希望 Agent 完成的任务…"/><div><small>Enter 发送 · Shift + Enter 换行</small><button className="primary" disabled={busy||!input.trim()} onClick={send}>{busy?"处理中…":"发送 ↑"}</button></div></div></>}</section></div></main>{error&&<button className="error" onClick={()=>setError("")}>{error} ×</button>}</div>;
}

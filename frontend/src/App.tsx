import {useCallback,useEffect,useState} from "react";
import {ApiError,api,keyStore,resumeRunStream,sendMessageStream} from "./api";
import {RunsPanel,ToolsPanel,OrganizationPanel} from "./panels";
import type {Agent,Conversation,Me,Message} from "./types";

type View = "chat" | "tools" | "runs" | "org";

export default function App(){
 const [apiKey,setApiKey]=useState(()=>keyStore.get());
 const [keyDraft,setKeyDraft]=useState("");
 const [view,setView]=useState<View>("chat");
 const [me,setMe]=useState<Me|null>(null);
 const [agents,setAgents]=useState<Agent[]>([]);
 const [agent,setAgent]=useState<Agent|null>(null);
 const [conversations,setConversations]=useState<Conversation[]>([]);
 const [conversation,setConversation]=useState<Conversation|null>(null);
 const [messages,setMessages]=useState<Message[]>([]);
 const [input,setInput]=useState("");
 const [busy,setBusy]=useState(false);
 const [error,setError]=useState("");
 // 流式输出的临时状态：完成时会替换为真实消息
 const [pendingText,setPendingText]=useState("");
 const [streamText,setStreamText]=useState("");
 const [streamTools,setStreamTools]=useState<string[]>([]);

 /** 401 意味着密钥不可用：清掉本地密钥并回到准入界面。 */
 const handleError=useCallback((err:unknown)=>{
  if(err instanceof ApiError && err.status===401){
   keyStore.set("");
   setApiKey("");
   setMe(null);
   setError("API Key 无效或已停用，请重新输入。");
   return;
  }
  setError(err instanceof Error ? err.message : String(err));
 },[]);

 const refresh=useCallback(async()=>{
  try{
   const page=await api.agents();
   setAgents(page.items);
   setAgent(current=>current ?? page.items[0] ?? null);
  }catch(err){handleError(err);}
 },[handleError]);

 useEffect(()=>{
  if(!apiKey) return;
  refresh();
  api.me().then(setMe).catch(handleError);
 },[apiKey,refresh,handleError]);

 useEffect(()=>{
  if(!agent) return;
  setConversation(null);
  setMessages([]);
  api.conversations(agent.id).then(page=>setConversations(page.items)).catch(handleError);
 },[agent?.id,handleError]);

 async function openConversation(target:Conversation){
  setConversation(target);
  try{setMessages((await api.messages(target.id)).items);}catch(err){handleError(err);}
 }

 /** 切换会话模式：写回服务端，保证刷新后仍是同一模式。 */
 async function changeMode(mode:"ask"|"plan"|"craft"){
  if(!conversation||conversation.mode===mode) return;
  try{
   const updated=await api.updateConversation(conversation.id,{mode});
   setConversation(updated);
   setConversations(old=>old.map(item=>item.id===updated.id?updated:item));
  }catch(err){handleError(err);}
 }

 async function addAgent(){
  const name=prompt("Agent 名称");
  if(!name?.trim()) return;
  try{
   const created=await api.createAgent({
    name:name.trim(),
    description:"AI assistant",
    system_prompt:"You are a helpful assistant. Answer in Chinese unless asked otherwise.",
    model:"gpt-4o-mini",
   });
   const page=await api.agents();
   setAgents(page.items);
   setAgent(created);
   setView("chat");
  }catch(err){handleError(err);}
 }

 async function addConversation(){
  if(!agent) return;
  try{
   const created=await api.createConversation(agent.id);
   setConversations(old=>[created,...old]);
   await openConversation(created);
  }catch(err){handleError(err);}
 }

 async function send(){
  if(!conversation||!input.trim()||busy) return;
  const text=input.trim();
  const conversationId=conversation.id;
  setInput("");
  setBusy(true);
  setError("");
  setPendingText(text);
  setStreamText("");
  setStreamTools([]);

  let runId="";
  let settled=false;
  const applyCompleted=(payload:{user_message:Message;assistant_message:Message})=>{
   settled=true;
   setMessages(old=>[...old,payload.user_message,payload.assistant_message]);
  };

  try{
   await sendMessageStream(conversationId,text,{
    onStarted:id=>{runId=id;},
    onDelta:chunk=>setStreamText(old=>old+chunk),
    onToolCall:tool=>setStreamTools(old=>[...old,tool]),
    onToolResult:(tool,status)=>setStreamTools(old=>[...old,`${tool}:${status}`]),
    onCompleted:applyCompleted,
    onError:(code,message)=>{
     settled=true;
     setError(`${code}: ${message}`);
     setInput(text);
    },
   });
  }catch(err){
   // 网络中断：服务端会把这次运行跑完，用回放接口把最终结果取回来。
   if(runId&&!settled&&!(err instanceof ApiError)){
    setError("连接中断，正在恢复本次运行的结果…");
    try{
     await resumeRunStream(runId,{
      onCompleted:applyCompleted,
      onEnd:(status,reason)=>{
       if(status==="succeeded") setError("");
       else setError(`本次运行未完成：${status}${reason?`（${reason}）`:""}`);
      },
     });
     return;
    }catch(resumeErr){
     handleError(resumeErr);
    }
   }
   handleError(err);
   setInput(text);
  }finally{
   setBusy(false);
   setPendingText("");
   setStreamText("");
   setStreamTools([]);
  }
 }

 function signIn(event:React.FormEvent){
  event.preventDefault();
  const key=keyDraft.trim();
  if(!key) return;
  keyStore.set(key);
  setKeyDraft("");
  setError("");
  setApiKey(key);
 }

 function signOut(){
  keyStore.set("");
  setApiKey("");
  setMe(null);
  setAgents([]);
  setAgent(null);
  setConversation(null);
  setMessages([]);
  setView("chat");
 }

 if(!apiKey){
  return <div className="shell"><main>
   <header><div><small>WORKSPACE / AUTH</small><h1>需要 API Key</h1></div></header>
   <section className="chat"><div className="welcome">
    <div>⌘</div>
    <h2>输入 API Key 继续</h2>
    <p>后端已默认开启认证。首次启动时，管理密钥会打印在服务日志里（形如 <code>oak_...</code>）。</p>
    <form onSubmit={signIn}>
     <input className="key-input" type="password" value={keyDraft} autoFocus
      onChange={e=>setKeyDraft(e.target.value)} placeholder="oak_..." />
     <button className="primary" type="submit" disabled={!keyDraft.trim()}>进入</button>
    </form>
    {error&&<p className="muted">{error}</p>}
   </div></section>
  </main></div>;
 }

 const activeAgent=agent;
 const title=view==="chat"
  ? (activeAgent?.name ?? "欢迎使用 Agent Platform")
  : view==="tools" ? "工具与策略"
  : view==="runs" ? "运行与审计"
  : "组织与成员";

 return <div className="shell">
  <aside className="sidebar">
   <div className="brand"><b>A</b><span><strong>Agent Platform</strong><small>Enterprise Workspace</small></span></div>

   <nav className="nav">
    <button className={view==="chat"?"active":""} onClick={()=>setView("chat")}>对话</button>
    <button className={view==="tools"?"active":""} onClick={()=>setView("tools")}>工具</button>
    <button className={view==="runs"?"active":""} onClick={()=>setView("runs")}>运行</button>
    <button className={view==="org"?"active":""} onClick={()=>setView("org")}>组织</button>
   </nav>

   <div className="label">AGENTS <button onClick={addAgent}>＋</button></div>
   {agents.map(item=><button key={item.id} className={`agent ${activeAgent?.id===item.id?"active":""}`}
    onClick={()=>{setAgent(item);setView("chat");}}>
    <i>{item.name[0]?.toUpperCase()}</i>
    <span><strong>{item.name}</strong><small>{item.model}</small></span>
   </button>)}
   {!agents.length&&<p className="muted">点击 + 创建第一个 Agent</p>}

   <footer>
    <span className="role-badge">{me?`${me.role} · ${me.name}`:"—"}</span>
    <button className="link" onClick={signOut}>切换 API Key</button>
   </footer>
  </aside>

  <main>
   <header>
    <div><small>WORKSPACE / {view.toUpperCase()}</small><h1>{title}</h1></div>
    {view==="chat"&&activeAgent&&<button className="primary" onClick={addConversation}>新建会话</button>}
   </header>

   {view==="tools"&&<ToolsPanel agents={agents} me={me} onError={handleError}/>}
   {view==="runs"&&<RunsPanel agents={agents} onError={handleError}/>}
   {view==="org"&&<OrganizationPanel me={me} onError={handleError}/>}

   {view==="chat"&&<div className="layout">
    <section className="sessions">
     <h3>会话</h3>
     <p className="muted">选择会话开始协作</p>
     {conversations.map(item=><button key={item.id} className={`session ${conversation?.id===item.id?"active":""}`}
      onClick={()=>openConversation(item)}>◷　{item.title}</button>)}
    </section>
    <section className="chat">
     {!conversation
      ? <div className="welcome">
         <div>✳</div>
         <h2>让 Agent 开始工作</h2>
         <p>创建或选择会话，然后输入第一个任务。</p>
         {activeAgent&&<button className="primary" onClick={addConversation}>创建会话</button>}
        </div>
      : <>
         <div className="chat-head">
          <div><b>{conversation.title}</b><small>{activeAgent?.name} · {activeAgent?.model}</small></div>
          <div className="mode-switch" title="Ask 只读问答 · Plan 先出计划 · Craft 直接执行">
           {(["ask","plan","craft"] as const).map(item=>(
            <button key={item} className={conversation.mode===item?"active":""}
             onClick={()=>changeMode(item)}>{item}</button>
           ))}
          </div>
         </div>
         <div className="messages">
          {messages.map(item=><div key={item.id} className={`message ${item.role}`}>
           <label>{item.role==="user"?"YOU":"AGENT"}</label>
           <p>{item.content}</p>
          </div>)}
          {busy&&<>
           <div className="message user"><label>YOU</label><p>{pendingText}</p></div>
           <div className="message assistant">
            <label>AGENT</label>
            <p>{streamText||"…"}</p>
            {streamTools.length>0&&<small className="muted">工具：{streamTools.join(" → ")}</small>}
           </div>
          </>}
         </div>
         <div className="composer">
          <textarea value={input} onChange={e=>setInput(e.target.value)}
           onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send();}}}
           placeholder="描述你希望 Agent 完成的任务…"/>
          <div>
           <small>Enter 发送 · Shift + Enter 换行</small>
           <button className="primary" disabled={busy||!input.trim()} onClick={send}>{busy?"处理中…":"发送 ↑"}</button>
          </div>
         </div>
        </>
     }
    </section>
   </div>}
  </main>

  {error&&<div className="error" role="alert">
   <b>操作失败</b>
   <span>{error}</span>
   <button className="link" onClick={()=>setError("")} aria-label="关闭">×</button>
  </div>}
 </div>;
}

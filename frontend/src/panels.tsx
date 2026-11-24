import {useCallback,useEffect,useRef,useState} from "react";
import {api,downloadArtifact} from "./api";
import type {Agent,AgentTool,Approval,Artifact,AuditLog,CronJob,KnowledgeBase,KnowledgeDocument,KnowledgeSearchHit,Me,Memory as MemoryRow,Membership,Run,RunEvent,Tenant,Tool,UsageBreakdown,UsageBucket,UsageOverview} from "./types";

const ROLES = ["viewer", "developer", "admin"] as const;
type Role = (typeof ROLES)[number];

const DEFAULT_SCHEMA = `{
  "type": "object",
  "properties": {
    "text": { "type": "string" }
  },
  "required": ["text"],
  "additionalProperties": false
}`;

const EXECUTOR_SCHEMA = `{
  "type": "object",
  "properties": {
    "command": { "type": "string" }
  },
  "required": ["command"],
  "additionalProperties": false
}`;

function shortId(id:string):string {
  return id.length > 14 ? `${id.slice(0,14)}…` : id;
}

function time(iso:string):string {
  return new Date(iso).toLocaleString();
}

function Decision({decision}:{decision:string|null}) {
  if(!decision) return <span className="muted">-</span>;
  return <span className={decision==="deny"?"no":"ok"}>{decision}</span>;
}

export function ToolsPanel({agents,me,onError}:{agents:Agent[];me:Me|null;onError:(err:unknown)=>void}) {
  const [tools,setTools]=useState<Tool[]>([]);
  const [audits,setAudits]=useState<AuditLog[]>([]);
  const [agentId,setAgentId]=useState(agents[0]?.id ?? "");
  const [agentTools,setAgentTools]=useState<AgentTool[]>([]);
  const [name,setName]=useState("");
  const [description,setDescription]=useState("");
  const [schema,setSchema]=useState(DEFAULT_SCHEMA);
  const [executionType,setExecutionType]=useState<"builtin"|"executor">("builtin");
  const [runtime,setRuntime]=useState<"shell"|"python">("shell");
  const [image,setImage]=useState("");
  const [needsApproval,setNeedsApproval]=useState(false);
  const [busy,setBusy]=useState(false);
  const [loading,setLoading]=useState(true);

  const canWriteTools = me?.permissions.includes("write:tools") ?? false;
  const canWritePolicy = me?.permissions.includes("write:policy") ?? false;

  const reload = useCallback(async () => {
    try {
      const [toolPage,auditPage] = await Promise.all([api.tools(),api.auditLogs(20)]);
      setTools(toolPage.items);
      setAudits(auditPage.items);
    } catch(err) { onError(err); }
    finally { setLoading(false); }
  },[onError]);

  const reloadAgentTools = useCallback(async (id:string) => {
    if(!id) { setAgentTools([]); return; }
    try { setAgentTools((await api.agentTools(id)).items); }
    catch(err) { onError(err); }
  },[onError]);

  useEffect(() => { reload(); },[reload]);
  useEffect(() => { reloadAgentTools(agentId); },[agentId,reloadAgentTools]);

  function changeType(next:"builtin"|"executor") {
    setExecutionType(next);
    setSchema(next==="executor" ? EXECUTOR_SCHEMA : DEFAULT_SCHEMA);
  }

  async function register(event:React.FormEvent) {
    event.preventDefault();
    if(!name.trim()) return;

    let parsed:Record<string,unknown>;
    try { parsed = JSON.parse(schema) as Record<string,unknown>; }
    catch { onError(new Error("input_schema 不是合法 JSON")); return; }

    setBusy(true);
    try {
      const payload:{name:string;description?:string;input_schema:Record<string,unknown>;execution_type:"builtin"|"executor";config?:Record<string,unknown>;requires_approval?:boolean} = {
        name:name.trim(), description:description.trim(), input_schema:parsed, execution_type:executionType,
        requires_approval: needsApproval,
      };
      if(executionType==="executor") {
        payload.config = {runtime, ...(image.trim() ? {image:image.trim()} : {})};
      }
      await api.createTool(payload);
      setName(""); setDescription("");
      await reload(); await reloadAgentTools(agentId);
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  async function toggle(tool:Tool) {
    setBusy(true);
    try {
      await api.updateTool(tool.id,{enabled:!tool.enabled});
      await reload(); await reloadAgentTools(agentId);
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  async function decide(toolName:string,effect:"allow"|"deny") {
    if(!agentId) return;
    setBusy(true);
    try {
      await api.createPolicyRule({agent_id:agentId,tool_name:toolName,effect,priority:100});
      await reloadAgentTools(agentId); await reload();
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  return <div className="layout panel-layout">
    <section className="sessions">
      <h3>注册工具</h3>
      {canWriteTools
        ? <form className="stack" onSubmit={register}>
            <input className="field" value={name} onChange={e => setName(e.target.value)} placeholder="工具名，例如 echo" />
            <input className="field" value={description} onChange={e => setDescription(e.target.value)} placeholder="描述" />
            <label className="muted" htmlFor="tool-type">类型</label>
            <select id="tool-type" className="field" value={executionType} onChange={e => changeType(e.target.value as "builtin"|"executor")}>
              <option value="builtin">builtin（内建实现）</option>
              <option value="executor">executor（Docker 隔离执行）</option>
            </select>
            {executionType==="executor" && <>
              <label className="muted" htmlFor="tool-runtime">运行时</label>
              <select id="tool-runtime" className="field" value={runtime} onChange={e => setRuntime(e.target.value as "shell"|"python")}>
                <option value="shell">shell</option>
                <option value="python">python</option>
              </select>
              <input className="field" value={image} onChange={e => setImage(e.target.value)} placeholder="镜像（可选，默认 python:3.12-slim）" />
            </>}
            <textarea className="field mono" rows={9} spellCheck={false} value={schema} onChange={e => setSchema(e.target.value)} />
            <small className="muted">
              {executionType==="executor"
                ? "命令由模型调用时给出，仍需策略放行；服务端要启用 EXECUTOR_ENABLED。"
                : "内建实现只有 echo 与 current_time；注册其它名字不会获得执行能力。"}
            </small>
            <label className="check">
          <input type="checkbox" checked={needsApproval}
            onChange={e => setNeedsApproval(e.target.checked)} />
          执行前需要人工审批
        </label>
        <button className="primary" type="submit" disabled={busy || !name.trim()}>注册工具</button>
          </form>
        : <p className="muted">当前角色只读，注册工具需要 admin。</p>}

      <h3>已注册</h3>
      {tools.map(tool => <div key={tool.id} className="row">
        <div>
          <b>{tool.name}</b>
          <small className="muted">{tool.execution_type}{tool.enabled ? "" : " · 已停用"}</small>
        </div>
        <button className="ghost" disabled={!canWriteTools || busy} onClick={() => toggle(tool)}>
          {tool.enabled ? "停用" : "启用"}
        </button>
      </div>)}
      {!tools.length && <p className="muted">{loading ? "加载中…" : "还没有注册任何工具。"}</p>}
    </section>

    <section className="chat">
      <div className="chat-head">
        <div><b>策略与审计</b><small>按 Agent 维护工具授权（默认拒绝）</small></div>
        <span>● {me?.role ?? "?"}</span>
      </div>
      <div className="panel-body">
        <label className="muted" htmlFor="agent-select">AGENT</label>
        <select id="agent-select" className="field" value={agentId} onChange={e => setAgentId(e.target.value)}>
          {agents.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>

        <table className="table">
          <thead>
            <tr><th>工具</th><th>可用</th><th>裁决原因</th><th>授权</th></tr>
          </thead>
          <tbody>
            {agentTools.map(item => <tr key={item.name}>
              <td>{item.name}</td>
              <td>{item.allowed ? <span className="ok">允许</span> : <span className="no">拒绝</span>}</td>
              <td className="muted small">{item.reason}</td>
              <td className="actions">
                <button className="ghost" disabled={!canWritePolicy || busy} onClick={() => decide(item.name,"allow")}>允许</button>
                <button className="ghost" disabled={!canWritePolicy || busy} onClick={() => decide(item.name,"deny")}>拒绝</button>
              </td>
            </tr>)}
          </tbody>
        </table>
        {!agentTools.length && <p className="muted">该 Agent 名下还没有可展示的工具。</p>}
        <h3>最近审计</h3>
        <ul className="feed">
          {audits.map(entry => <li key={entry.id}>
            <code>{entry.action}</code> <Decision decision={entry.decision} /> {entry.resource}
            <small className="muted"> · {entry.actor} · {time(entry.created_at)}</small>
          </li>)}
        </ul>
        {!audits.length && <p className="muted">{loading ? "加载中…" : "暂无审计记录。"}</p>}
      </div>
    </section>
  </div>;
}

export function RunsPanel({agents,onError}:{agents:Agent[];onError:(err:unknown)=>void}) {
  const [runs,setRuns]=useState<Run[]>([]);
  const [agentId,setAgentId]=useState("");
  const [selected,setSelected]=useState<Run|null>(null);
  const [events,setEvents]=useState<RunEvent[]>([]);
  const [loading,setLoading]=useState(true);

  const reload = useCallback(async () => {
    try { setRuns((await api.runs(agentId || undefined,30)).items); }
    catch(err) { onError(err); }
    finally { setLoading(false); }
  },[agentId,onError]);

  useEffect(() => { reload(); },[reload]);

  async function open(run:Run) {
    setSelected(run);
    try { setEvents((await api.runEvents(run.id)).items); }
    catch(err) { onError(err); }
  }

  return <div className="layout panel-layout">
    <section className="sessions">
      <h3>运行记录</h3>
      <select className="field" value={agentId} onChange={e => setAgentId(e.target.value)}>
        <option value="">全部 Agent</option>
        {agents.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
      <button className="ghost" onClick={reload}>刷新</button>
      {runs.map(run => <button key={run.id} className={`session ${selected?.id===run.id?"active":""}`} onClick={() => open(run)}>
        <span className={run.status==="succeeded"?"ok":run.status==="failed"?"no":"muted"}>●</span> {shortId(run.id)}
        <small className="muted"> · {time(run.started_at)}</small>
      </button>)}
      {!runs.length && <p className="muted">{loading ? "加载中…" : "还没有运行记录。发一条消息即可产生。"}</p>}
    </section>

    <section className="chat">
      <div className="chat-head">
        <div><b>{selected ? shortId(selected.id) : "选择一条运行"}</b>
          <small>{selected ? `${selected.status} · ${selected.iterations} 轮 · ${selected.tool_calls} 次工具调用` : "查看事件流"}</small>
        </div>
        <span>● {selected?.status ?? "idle"}</span>
      </div>
      <div className="panel-body">
        {selected && <>
          {selected.error_code && <p className="muted">错误：<span className="no">{selected.error_code}</span> {selected.error_message}</p>}
          <h3>事件流</h3>
          <ol className="feed">
            {events.map(event => <li key={event.id}>
              <code>#{event.seq} {event.type}</code>
              <small className="muted"> · {time(event.created_at)}</small>
              <pre className="payload">{JSON.stringify(event.payload)}</pre>
            </li>)}
          </ol>
          {!events.length && <p className="muted">该运行没有事件。</p>}
        </>}
        {!selected && <p className="muted">从左侧选择一条运行记录，查看它做过什么。</p>}
      </div>
    </section>
  </div>;
}

export function OrganizationPanel({me,onError}:{me:Me|null;onError:(err:unknown)=>void}) {
  const [tenant,setTenant]=useState<Tenant|null>(null);
  const [members,setMembers]=useState<Membership[]>([]);
  const [subject,setSubject]=useState("");
  const [role,setRole]=useState<Role>("viewer");
  const [newTenantId,setNewTenantId]=useState("");
  const [newTenantName,setNewTenantName]=useState("");
  const [busy,setBusy]=useState(false);
  const [loading,setLoading]=useState(true);

  const canManage = me?.permissions.includes("write:members") ?? false;

  const reload = useCallback(async () => {
    try {
      const [current,page] = await Promise.all([api.currentTenant(),api.memberships()]);
      setTenant(current);
      setMembers(page.items);
    } catch(err) { onError(err); }
    finally { setLoading(false); }
  },[onError]);

  useEffect(() => { reload(); },[reload]);

  async function addMember(event:React.FormEvent) {
    event.preventDefault();
    if(!subject.trim()) return;
    setBusy(true);
    try {
      await api.addMembership({subject:subject.trim(),role});
      setSubject("");
      await reload();
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  async function changeRole(membership:Membership,next:Role) {
    setBusy(true);
    try {
      await api.updateMembership(membership.id,next);
      await reload();
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  async function remove(membership:Membership) {
    setBusy(true);
    try {
      await api.removeMembership(membership.id);
      await reload();
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  async function createTenant(event:React.FormEvent) {
    event.preventDefault();
    if(!newTenantId.trim()) return;
    setBusy(true);
    try {
      await api.createTenant({
        id:newTenantId.trim(),
        name:newTenantName.trim() || newTenantId.trim(),
      });
      setNewTenantId("");
      setNewTenantName("");
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  return <div className="layout panel-layout">
    <section className="sessions">
      <h3>当前租户</h3>
      {tenant
        ? <div className="row">
            <div>
              <b>{tenant.name}</b>
              <small className="muted"><code>{tenant.id}</code> · {tenant.status}</small>
            </div>
            <span className={tenant.status==="active"?"ok":"no"}>●</span>
          </div>
        : <p className="muted">加载中…</p>}

      <h3>开通新租户</h3>
      {canManage
        ? <form className="stack" onSubmit={createTenant}>
            <input className="field" value={newTenantId} onChange={e => setNewTenantId(e.target.value)}
              placeholder="租户 ID，例如 acme" />
            <input className="field" value={newTenantName} onChange={e => setNewTenantName(e.target.value)}
              placeholder="显示名称（可选）" />
            <small className="muted">
              开通后还需要为该租户签发 API Key（POST /api-keys 带 tenant_id）才能使用。
            </small>
            <button className="primary" type="submit" disabled={busy || !newTenantId.trim()}>开通租户</button>
          </form>
        : <p className="muted">当前角色无权开通租户（需要 admin）。</p>}
    </section>

    <section className="chat">
      <div className="chat-head">
        <div><b>成员</b><small>OIDC 用户的角色以此处为准，会覆盖 token 里的 claim</small></div>
        <span>● {me?.role ?? "?"}</span>
      </div>
      <div className="panel-body">
        {canManage
          ? <form className="stack" onSubmit={addMember}>
              <div className="inline-form">
                <input className="field" value={subject} onChange={e => setSubject(e.target.value)}
                  placeholder="成员标识，例如 oidc:user-1" />
                <select className="field compact" value={role} onChange={e => setRole(e.target.value as Role)}>
                  {ROLES.map(item => <option key={item} value={item}>{item}</option>)}
                </select>
                <button className="primary" type="submit" disabled={busy || !subject.trim()}>加入</button>
              </div>
            </form>
          : <p className="muted">当前角色无权管理成员（需要 admin）。</p>}

        <table className="table">
          <thead>
            <tr><th>成员</th><th>角色</th><th>加入时间</th><th></th></tr>
          </thead>
          <tbody>
            {members.map(item => <tr key={item.id}>
              <td><code>{item.subject}</code></td>
              <td>
                {canManage
                  ? <select className="field compact" value={item.role} disabled={busy}
                      onChange={e => changeRole(item,e.target.value as Role)}>
                      {ROLES.map(option => <option key={option} value={option}>{option}</option>)}
                    </select>
                  : item.role}
              </td>
              <td className="muted small">{time(item.created_at)}</td>
              <td className="actions">
                <button className="ghost" disabled={!canManage || busy} onClick={() => remove(item)}>移除</button>
              </td>
            </tr>)}
          </tbody>
        </table>
        {!members.length && <p className="muted">{loading ? "加载中…" : "该租户还没有成员。"}</p>}
      </div>
    </section>
  </div>;
}

export function CronPanel({agents,onError}:{agents:Agent[];onError:(err:unknown)=>void}) {
  const [agentId,setAgentId]=useState(agents[0]?.id ?? "");
  const [jobs,setJobs]=useState<CronJob[]>([]);
  const [loading,setLoading]=useState(true);
  const [busy,setBusy]=useState(false);
  const [name,setName]=useState("每日汇报");
  const [prompt,setPrompt]=useState("用三句话总结今天的进展。");
  const [schedule,setSchedule]=useState("0 9 * * *");

  useEffect(()=>{ if(!agentId&&agents[0]) setAgentId(agents[0].id); },[agentId,agents]);

  const reload = useCallback(async () => {
    if(!agentId) return;
    try { setJobs((await api.cronJobs(agentId)).items); }
    catch(err) { onError(err); }
    finally { setLoading(false); }
  },[agentId,onError]);

  useEffect(()=>{ reload(); },[reload]);

  async function create(event:React.FormEvent){
    event.preventDefault();
    if(!agentId||!prompt.trim()||!schedule.trim()) return;
    setBusy(true);
    try{
      await api.createCronJob(agentId,{name:name.trim()||"scheduled job",prompt:prompt.trim(),schedule:schedule.trim()});
      await reload();
    }catch(err){onError(err);}
    finally{setBusy(false);}
  }

  async function toggle(job:CronJob){
    setBusy(true);
    try{ await api.updateCronJob(job.id,{enabled:!job.enabled}); await reload(); }
    catch(err){onError(err);}
    finally{setBusy(false);}
  }

  async function runNow(job:CronJob){
    setBusy(true);
    try{ await api.runCronJobNow(job.id); await reload(); }
    catch(err){onError(err);}
    finally{setBusy(false);}
  }

  async function remove(job:CronJob){
    setBusy(true);
    try{ await api.deleteCronJob(job.id); await reload(); }
    catch(err){onError(err);}
    finally{setBusy(false);}
  }

  return <div className="layout panel-layout">
    <section className="sessions">
      <h3>新建定时任务</h3>
      <form className="stack" onSubmit={create}>
        <select className="field" value={agentId} onChange={e=>setAgentId(e.target.value)}>
          {agents.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <input className="field" value={name} onChange={e=>setName(e.target.value)} placeholder="任务名称" />
        <input className="field" value={schedule} onChange={e=>setSchedule(e.target.value)}
          placeholder="cron 表达式，例如 0 9 * * *" />
        <textarea className="field" rows={3} value={prompt} onChange={e=>setPrompt(e.target.value)}
          placeholder="每次执行时发给 Agent 的提示词" />
        <small className="muted">5 段 cron（分 时 日 月 周），按 UTC 解释；也支持 @daily / @hourly。</small>
        <button className="primary" type="submit" disabled={busy || !prompt.trim() || !schedule.trim()}>
          创建任务
        </button>
      </form>
    </section>

    <section className="chat">
      <div className="chat-head">
        <div><b>定时任务</b><small>由服务端调度循环触发，进程停止则不会执行</small></div>
      </div>
      <div className="panel-body">
        <table className="table">
          <thead>
            <tr><th>名称</th><th>表达式</th><th>下次执行</th><th>上次状态</th><th></th></tr>
          </thead>
          <tbody>
            {jobs.map(job=><tr key={job.id}>
              <td>{job.name}</td>
              <td><code>{job.schedule}</code></td>
              <td className="muted small">{time(job.next_run_at)}</td>
              <td>
                <span className={job.last_status==="succeeded"?"ok":job.last_status?"no":""}>
                  {job.last_status ?? "未执行"}
                </span>
              </td>
              <td className="actions">
                <button className="ghost" disabled={busy} onClick={()=>runNow(job)}>立即执行</button>
                <button className="ghost" disabled={busy} onClick={()=>toggle(job)}>
                  {job.enabled?"停用":"启用"}
                </button>
                <button className="ghost" disabled={busy} onClick={()=>remove(job)}>删除</button>
              </td>
            </tr>)}
          </tbody>
        </table>
        {!jobs.length && <p className="muted">{loading ? "加载中…" : "还没有定时任务。"}</p>}
      </div>
    </section>
  </div>;
}

export function ApprovalPanel({onError,onChanged}:{onError:(err:unknown)=>void;onChanged?:()=>void}) {
  const [items,setItems]=useState<Approval[]>([]);
  const [loading,setLoading]=useState(true);
  const [busy,setBusy]=useState(false);

  const reload = useCallback(async () => {
    try { setItems((await api.approvals()).items); }
    catch(err) { onError(err); }
    finally { setLoading(false); }
  },[onError]);

  useEffect(()=>{ reload(); },[reload]);

  async function decide(item:Approval, action:"approve"|"reject"){
    setBusy(true);
    try{
      if(action==="approve") await api.approve(item.id);
      else await api.reject(item.id,"前端拒绝");
      await reload();
      onChanged?.();
    }catch(err){onError(err);}
    finally{setBusy(false);}
  }

  return <div className="layout panel-layout">
    <section className="sessions">
      <h3>人工审批</h3>
      <p className="muted">
        标记了「需要审批」的工具，在真正执行前会停下来等人确认。
        批准后工具执行，并从检查点续跑同一次运行；拒绝则把结果告诉模型，让它自行收尾。
      </p>
      <button className="ghost" onClick={()=>reload()} disabled={busy}>刷新</button>
    </section>

    <section className="chat">
      <div className="chat-head">
        <div><b>待审批</b><small>{items.length} 条等待处理</small></div>
      </div>
      <div className="panel-body">
        {items.map(item=><div className="approval-card" key={item.id}>
          <div className="approval-head">
            <b>{item.tool_name}</b>
            <small className="muted">{time(item.created_at)}</small>
          </div>
          <pre className="approval-args">{JSON.stringify(item.arguments,null,2)}</pre>
          <div className="actions">
            <button className="primary" disabled={busy} onClick={()=>decide(item,"approve")}>批准并执行</button>
            <button className="ghost" disabled={busy} onClick={()=>decide(item,"reject")}>拒绝</button>
          </div>
        </div>)}
        {!items.length && <p className="muted">{loading ? "加载中…" : "没有待审批的调用。"}</p>}
      </div>
    </section>
  </div>;
}

export function MemoryPanel({agents,onError}:{agents:Agent[];onError:(err:unknown)=>void}) {
  const [items,setItems]=useState<MemoryRow[]>([]);
  const [content,setContent]=useState("");
  const [scope,setScope]=useState("");
  const [query,setQuery]=useState("");
  const [busy,setBusy]=useState(false);
  const [loading,setLoading]=useState(true);

  const reload = useCallback(async (q?:string) => {
    try { setItems((await api.memories(q)).items); }
    catch(err) { onError(err); }
    finally { setLoading(false); }
  },[onError]);

  useEffect(()=>{ reload(); },[reload]);

  async function add(event:React.FormEvent){
    event.preventDefault();
    if(!content.trim()) return;
    setBusy(true);
    try{
      await api.createMemory({content:content.trim(), agent_id:scope||undefined});
      setContent("");
      await reload(query||undefined);
    }catch(err){onError(err);}
    finally{setBusy(false);}
  }

  async function remove(item:MemoryRow){
    setBusy(true);
    try{ await api.deleteMemory(item.id); await reload(query||undefined); }
    catch(err){onError(err);}
    finally{setBusy(false);}
  }

  return <div className="layout panel-layout">
    <section className="sessions">
      <h3>记一条</h3>
      <form className="stack" onSubmit={add}>
        <textarea className="field" rows={4} value={content} onChange={e=>setContent(e.target.value)}
          placeholder="值得跨会话记住的事实，例如：项目统一使用深色主题" />
        <select className="field" value={scope} onChange={e=>setScope(e.target.value)}>
          <option value="">租户级（所有 Agent 可见）</option>
          {agents.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <button className="primary" type="submit" disabled={busy || !content.trim()}>保存记忆</button>
      </form>
      <h3>检索</h3>
      <div className="inline-form">
        <input className="field" value={query} onChange={e=>setQuery(e.target.value)} placeholder="按关键词过滤" />
        <button className="ghost" type="button" onClick={()=>reload(query||undefined)} disabled={busy}>搜索</button>
      </div>
      <p className="muted">
        运行时按词法相关度自动注入相关条目（最多 5 条）。这是关键词召回，不是语义检索。
      </p>
    </section>

    <section className="chat">
      <div className="chat-head">
        <div><b>长期记忆</b><small>{items.length} 条</small></div>
      </div>
      <div className="panel-body">
        <table className="table">
          <thead><tr><th>内容</th><th>范围</th><th>时间</th><th></th></tr></thead>
          <tbody>
            {items.map(item=><tr key={item.id}>
              <td>{item.content}</td>
              <td className="muted small">{item.agent_id ? "Agent" : "租户"}</td>
              <td className="muted small">{time(item.created_at)}</td>
              <td className="actions">
                <button className="ghost" disabled={busy} onClick={()=>remove(item)}>删除</button>
              </td>
            </tr>)}
          </tbody>
        </table>
        {!items.length && <p className="muted">{loading ? "加载中…" : "还没有记忆。"}</p>}
      </div>
    </section>
  </div>;
}

export function KnowledgePanel({me,onError}:{me:Me|null;onError:(err:unknown)=>void}) {
  const [bases,setBases]=useState<KnowledgeBase[]>([]);
  const [selectedId,setSelectedId]=useState("");
  const [documents,setDocuments]=useState<KnowledgeDocument[]>([]);
  const [hits,setHits]=useState<KnowledgeSearchHit[]>([]);
  const [baseName,setBaseName]=useState("");
  const [baseDescription,setBaseDescription]=useState("");
  const [documentName,setDocumentName]=useState("");
  const [documentContent,setDocumentContent]=useState("");
  const [query,setQuery]=useState("");
  const [searched,setSearched]=useState(false);
  const [busy,setBusy]=useState(false);
  const [loading,setLoading]=useState(true);

  // 知识库的写权限与 Agent 同一档（后端用 write:agents 校验）
  const canWrite = me?.permissions.includes("write:agents") ?? false;
  const selected = bases.find(item=>item.id===selectedId) ?? null;

  const reloadBases = useCallback(async () => {
    try { setBases((await api.knowledgeBases()).items); }
    catch(err) { onError(err); }
    finally { setLoading(false); }
  },[onError]);

  const reloadDocuments = useCallback(async (baseId:string) => {
    if(!baseId) { setDocuments([]); return; }
    try { setDocuments((await api.knowledgeDocuments(baseId)).items); }
    catch(err) { onError(err); }
  },[onError]);

  useEffect(()=>{ reloadBases(); },[reloadBases]);
  useEffect(()=>{ reloadDocuments(selectedId); setHits([]); setSearched(false); },[selectedId,reloadDocuments]);

  async function createBase(event:React.FormEvent) {
    event.preventDefault();
    if(!baseName.trim()) return;
    setBusy(true);
    try {
      const created = await api.createKnowledgeBase({name:baseName.trim(), description:baseDescription.trim()});
      setBaseName(""); setBaseDescription("");
      await reloadBases();
      setSelectedId(created.id);
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  async function removeBase(base:KnowledgeBase) {
    setBusy(true);
    try {
      await api.deleteKnowledgeBase(base.id);
      if(selectedId===base.id) setSelectedId("");
      await reloadBases();
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  async function addDocument(event:React.FormEvent) {
    event.preventDefault();
    if(!selectedId || !documentName.trim() || !documentContent.trim()) return;
    setBusy(true);
    try {
      await api.addKnowledgeDocument(selectedId, {name:documentName.trim(), content:documentContent});
      setDocumentName(""); setDocumentContent("");
      await reloadDocuments(selectedId);
      await reloadBases();
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  async function removeDocument(document:KnowledgeDocument) {
    setBusy(true);
    try {
      await api.deleteKnowledgeDocument(selectedId, document.id);
      await reloadDocuments(selectedId);
      await reloadBases();
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  async function runSearch(event:React.FormEvent) {
    event.preventDefault();
    if(!selectedId || !query.trim()) return;
    setBusy(true);
    try {
      const result = await api.searchKnowledge(selectedId, query.trim());
      setHits(result.hits);
      setSearched(true);
    } catch(err) { onError(err); }
    finally { setBusy(false); }
  }

  return <div className="layout panel-layout">
    <section className="sessions">
      <h3>新建知识库</h3>
      {canWrite
        ? <form className="stack" onSubmit={createBase}>
            <input className="field" value={baseName} onChange={e=>setBaseName(e.target.value)}
              placeholder="知识库名称，例如 运维手册" />
            <input className="field" value={baseDescription} onChange={e=>setBaseDescription(e.target.value)}
              placeholder="描述（可选）" />
            <button className="primary" type="submit" disabled={busy || !baseName.trim()}>创建知识库</button>
          </form>
        : <p className="muted">当前角色只读，创建知识库需要 developer。</p>}

      <h3>知识库</h3>
      {bases.map(base=><div key={base.id} className="row">
        <button className={`base-pick ${selectedId===base.id?"active":""}`} onClick={()=>setSelectedId(base.id)}>
          <b>{base.name}</b>
          <small className="muted">{base.chunk_count} 块 · {base.embedding_model}</small>
        </button>
        <button className="ghost" disabled={!canWrite || busy} onClick={()=>removeBase(base)}>删除</button>
      </div>)}
      {!bases.length && <p className="muted">{loading ? "加载中…" : "还没有知识库。"}</p>}
    </section>

    <section className="chat">
      <div className="chat-head">
        <div><b>{selected ? selected.name : "选择知识库"}</b>
          <small>检索单位是块（chunk），不是整篇文档</small>
        </div>
        <span>● {me?.role ?? "?"}</span>
      </div>
      <div className="panel-body">
        {selected
          ? <>
            <h3>导入文档</h3>
            {canWrite
              ? <form className="stack" onSubmit={addDocument}>
                  <input className="field" value={documentName} onChange={e=>setDocumentName(e.target.value)}
                    placeholder="文档名称" />
                  <textarea className="field" rows={6} value={documentContent} onChange={e=>setDocumentContent(e.target.value)}
                    placeholder="粘贴纯文本内容，服务端自动切块并向量化" />
                  <button className="primary" type="submit" disabled={busy || !documentName.trim() || !documentContent.trim()}>
                    导入文档
                  </button>
                </form>
              : <p className="muted">当前角色只读，导入文档需要 developer。</p>}

            <h3>文档</h3>
            <table className="table">
              <thead><tr><th>名称</th><th>块数</th><th>时间</th><th></th></tr></thead>
              <tbody>
                {documents.map(item=><tr key={item.id}>
                  <td>{item.name}</td>
                  <td className="muted small">{item.chunk_count}</td>
                  <td className="muted small">{time(item.created_at)}</td>
                  <td className="actions">
                    <button className="ghost" disabled={!canWrite || busy} onClick={()=>removeDocument(item)}>删除</button>
                  </td>
                </tr>)}
              </tbody>
            </table>
            {!documents.length && <p className="muted">{loading ? "加载中…" : "该知识库还没有文档。"}</p>}

            <h3>检索</h3>
            <form className="inline-form" onSubmit={runSearch}>
              <input className="field" value={query} onChange={e=>setQuery(e.target.value)}
                placeholder="例如 预算超支怎么办" />
              <button className="ghost" type="submit" disabled={busy || !query.trim()}>搜索</button>
            </form>
            <small className="muted">
              默认按词面覆盖率排序（服务端可切余弦）；命中要求至少共享一个词面特征，同义改写不一定命中。
            </small>

            {hits.length>0 && <ul className="feed">
              {hits.map((hit,index)=><li key={`${hit.document}-${hit.seq}-${index}`}>
                <code>{hit.document} #{hit.seq}</code> <span className="muted small">score {hit.score}</span>
                <pre className="payload">{hit.text}</pre>
              </li>)}
            </ul>}
            {searched && !hits.length && <p className="muted">没有命中任何块。</p>}
          </>
          : <p className="muted">从左侧选择一个知识库，导入文档并检索。</p>}
      </div>
    </section>
  </div>;
}

const USAGE_DIMENSIONS = [
  { value: "agent", label: "按 Agent" },
  { value: "model", label: "按模型" },
  { value: "tool", label: "按工具" },
] as const;

const USAGE_BUCKETS = [
  { value: "day", label: "按天" },
  { value: "week", label: "按周" },
  { value: "month", label: "按月" },
] as const;

export function UsagePanel({onError}:{onError:(err:unknown)=>void}) {
  const [days,setDays]=useState(30);
  const [bucket,setBucket]=useState("day");
  const [dimension,setDimension]=useState("agent");
  const [overview,setOverview]=useState<UsageOverview|null>(null);
  const [timeline,setTimeline]=useState<UsageBucket[]>([]);
  const [breakdown,setBreakdown]=useState<UsageBreakdown[]>([]);
  const [busy,setBusy]=useState(false);
  const [loading,setLoading]=useState(true);
  const chartRef=useRef<HTMLDivElement|null>(null);

  const reload = useCallback(async () => {
    setBusy(true);
    try {
      const [total,series,parts] = await Promise.all([
        api.usageOverview(days),
        api.usageTimeline(days,bucket),
        api.usageBreakdown(days,dimension),
      ]);
      setOverview(total);
      setTimeline(series);
      setBreakdown(parts);
    } catch(err) { onError(err); }
    finally { setLoading(false); setBusy(false); }
  },[days,bucket,dimension,onError]);

  useEffect(()=>{ reload(); },[reload]);

  // 时间序列按时间正序，最新的在下面；默认滚到底，否则有数据的那天看不到
  useEffect(()=>{
    const node=chartRef.current;
    if(node) node.scrollTop=node.scrollHeight;
  },[timeline]);

  const maxRuns = Math.max(1, ...timeline.map(item=>item.runs));
  // 一年按天分桶会有三百多行，界面只画最近的一段
  const visible = timeline.length>60 ? timeline.slice(-60) : timeline;
  const dimensionLabel = USAGE_DIMENSIONS.find(item=>item.value===dimension)?.label ?? "分解";

  return <div className="layout panel-layout">
    <section className="sessions">
      <h3>统计窗口</h3>
      <div className="stack">
        <label className="muted" htmlFor="usage-days">时间范围</label>
        <select id="usage-days" className="field" value={days} onChange={e=>setDays(Number(e.target.value))}>
          <option value={7}>最近 7 天</option>
          <option value={30}>最近 30 天</option>
          <option value={90}>最近 90 天</option>
          <option value={365}>最近 365 天</option>
        </select>
        <label className="muted" htmlFor="usage-bucket">时间粒度</label>
        <select id="usage-bucket" className="field" value={bucket} onChange={e=>setBucket(e.target.value)}>
          {USAGE_BUCKETS.map(item=><option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
        <label className="muted" htmlFor="usage-dimension">分解维度</label>
        <select id="usage-dimension" className="field" value={dimension} onChange={e=>setDimension(e.target.value)}>
          {USAGE_DIMENSIONS.map(item=><option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
      </div>
      <p className="muted">
        口径是运行次数与字符数，不是 token——模型网关目前不解析 usage，
        所以这个报表回答不了「花了多少钱」。
      </p>
      <button className="ghost" onClick={reload} disabled={busy}>刷新</button>
    </section>

    <section className="chat">
      <div className="chat-head">
        <div><b>用量</b><small>最近 {days} 天</small></div>
        <span>● {loading ? "…" : "ok"}</span>
      </div>
      <div className="panel-body">
        {overview
          ? <>
            <div className="stat-grid">
              <div className="stat"><b>{overview.runs}</b><small>运行次数</small></div>
              <div className="stat"><b>{overview.succeeded}</b><small>成功</small></div>
              <div className="stat"><b className={overview.failed?"no":""}>{overview.failed}</b><small>失败</small></div>
              <div className="stat"><b>{overview.tool_calls}</b><small>工具调用</small></div>
              <div className="stat"><b>{(overview.avg_duration_ms/1000).toFixed(1)}s</b><small>平均耗时</small></div>
              <div className="stat"><b>{overview.agents}</b><small>活跃 Agent</small></div>
            </div>
            <small className="muted">
              输出 {overview.answer_chars} 字符 · 输入 {overview.prompt_chars} 字符 ·
              覆盖 {overview.models} 个模型 / {overview.tools} 个工具 ·
              日配额 {overview.daily_run_quota || "不限"} 次
            </small>

            <h3>时间序列</h3>
            <div className="bar-chart" ref={chartRef}>
              {visible.map(item=><div key={item.key} className="bar-row">
                <span className="bar-label">{item.key}</span>
                <progress className="bar-fill" value={item.runs} max={maxRuns}/>
                <span className="bar-value">{item.runs}</span>
              </div>)}
            </div>
            {!visible.length && <p className="muted">窗口内没有运行记录。</p>}

            <h3>{dimensionLabel}</h3>
            <table className="table">
              <thead><tr><th>名称</th><th>运行</th><th>工具调用</th><th>错误</th><th>平均耗时</th></tr></thead>
              <tbody>
                {breakdown.map(item=><tr key={item.key}>
                  <td>
                    {item.label}
                    {dimension==="agent" && <small className="muted"> · {item.key.slice(0,8)}</small>}
                  </td>
                  <td>{item.runs}</td>
                  <td>{item.tool_calls}</td>
                  <td className={item.errors?"no":"muted"}>{item.errors}</td>
                  <td className="muted small">{(item.avg_duration_ms/1000).toFixed(1)}s</td>
                </tr>)}
              </tbody>
            </table>
            {!breakdown.length && <p className="muted">窗口内没有可分解的记录。</p>}
          </>
          : <p className="muted">{loading ? "加载中…" : "没有用量数据。"}</p>}
      </div>
    </section>
  </div>;
}

function fileSize(bytes:number):string {
  if(bytes<1024) return `${bytes} B`;
  if(bytes<1024*1024) return `${(bytes/1024).toFixed(1)} KB`;
  return `${(bytes/1024/1024).toFixed(1)} MB`;
}

export function ArtifactPanel({onError}:{onError:(err:unknown)=>void}) {
  const [items,setItems]=useState<Artifact[]>([]);
  const [total,setTotal]=useState(0);
  const [toolName,setToolName]=useState("");
  const [runId,setRunId]=useState("");
  const [loading,setLoading]=useState(true);
  const [busy,setBusy]=useState(false);
  const [copied,setCopied]=useState("");

  const reload=useCallback(async()=>{
    setBusy(true);
    try{
      const page=await api.artifacts({
        tool_name:toolName.trim()||undefined,
        run_id:runId.trim()||undefined,
        limit:100,
      });
      setItems(page.items);
      setTotal(page.total);
    }catch(err){onError(err);}
    finally{setLoading(false);setBusy(false);}
  },[toolName,runId,onError]);

  useEffect(()=>{reload();},[reload]);

  async function download(item:Artifact){
    try{await downloadArtifact(item.id,item.path.split("/").pop() ?? item.path);}
    catch(err){onError(err);}
  }

  /** 复制产物 id：它就是输入挂载参数（file_id）要填的值。 */
  async function copyId(item:Artifact){
    try{
      if(!navigator.clipboard) throw new Error("当前环境不支持剪贴板");
      await navigator.clipboard.writeText(item.id);
      setCopied(item.id);
      setTimeout(()=>setCopied(current=>current===item.id?"":current),1500);
    }catch(err){onError(err);}
  }

  return <div className="layout panel-layout">
    <section className="sessions">
      <h3>筛选</h3>
      <div className="stack">
        <label className="muted" htmlFor="artifact-tool">工具名</label>
        <input id="artifact-tool" className="field" value={toolName}
          placeholder="留空表示全部" onChange={e=>setToolName(e.target.value)}/>
        <label className="muted" htmlFor="artifact-run">运行 ID</label>
        <input id="artifact-run" className="field" value={runId}
          placeholder="留空表示全部" onChange={e=>setRunId(e.target.value)}/>
      </div>
      <p className="muted">
        产物来自隔离执行器。想把它喂给下一次执行，就复制这里的 id，
        填进工具声明的输入参数（<code>config.inputs</code> 里的 <code>argument</code>）。
      </p>
      <button className="ghost" onClick={reload} disabled={busy}>刷新</button>
    </section>

    <section className="chat">
      <div className="chat-head">
        <div><b>产物</b><small>共 {total} 个</small></div>
        <span>● {loading ? "…" : "ok"}</span>
      </div>
      <div className="panel-body">
        <table className="table">
          <thead><tr>
            <th>路径</th><th>工具</th><th>大小</th><th>生成时间</th><th>产物 ID</th><th>操作</th>
          </tr></thead>
          <tbody>
            {items.map(item=><tr key={item.id}>
              <td>{item.path}</td>
              <td className="muted">{item.tool_name}</td>
              <td className="muted">{fileSize(item.size_bytes)}</td>
              <td className="muted small">{time(item.created_at)}</td>
              <td><code className="artifact-id" title={item.id}>{item.id.slice(0,8)}</code></td>
              <td>
                <div className="actions">
                  <button className="ghost" onClick={()=>download(item)}>下载</button>
                  <button className="ghost" onClick={()=>copyId(item)}>
                    {copied===item.id ? "已复制" : "复制 ID"}
                  </button>
                </div>
              </td>
            </tr>)}
          </tbody>
        </table>
        {!items.length && <p className="muted">{loading ? "加载中…" : "还没有产物。"}</p>}
      </div>
    </section>
  </div>;
}

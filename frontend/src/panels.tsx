import {useCallback,useEffect,useState} from "react";
import {api} from "./api";
import type {Agent,AgentTool,AuditLog,Me,Membership,Run,RunEvent,Tenant,Tool} from "./types";

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
      const payload:{name:string;description?:string;input_schema:Record<string,unknown>;execution_type:"builtin"|"executor";config?:Record<string,unknown>} = {
        name:name.trim(), description:description.trim(), input_schema:parsed, execution_type:executionType,
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

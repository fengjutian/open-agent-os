import {useCallback,useEffect,useState} from "react";
import {api} from "./api";
import type {Agent,AgentTool,AuditLog,Me,Run,RunEvent,Tool} from "./types";

const DEFAULT_SCHEMA = `{
  "type": "object",
  "properties": {
    "text": { "type": "string" }
  },
  "required": ["text"],
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
  const [busy,setBusy]=useState(false);

  const canWriteTools = me?.permissions.includes("write:tools") ?? false;
  const canWritePolicy = me?.permissions.includes("write:policy") ?? false;

  const reload = useCallback(async () => {
    try {
      const [toolPage,auditPage] = await Promise.all([api.tools(),api.auditLogs(20)]);
      setTools(toolPage.items);
      setAudits(auditPage.items);
    } catch(err) { onError(err); }
  },[onError]);

  const reloadAgentTools = useCallback(async (id:string) => {
    if(!id) { setAgentTools([]); return; }
    try { setAgentTools((await api.agentTools(id)).items); }
    catch(err) { onError(err); }
  },[onError]);

  useEffect(() => { reload(); },[reload]);
  useEffect(() => { reloadAgentTools(agentId); },[agentId,reloadAgentTools]);

  async function register(event:React.FormEvent) {
    event.preventDefault();
    if(!name.trim()) return;

    let parsed:Record<string,unknown>;
    try { parsed = JSON.parse(schema) as Record<string,unknown>; }
    catch { onError(new Error("input_schema 不是合法 JSON")); return; }

    setBusy(true);
    try {
      await api.createTool({name:name.trim(),description:description.trim(),input_schema:parsed});
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
            <textarea className="field mono" rows={9} spellCheck={false} value={schema} onChange={e => setSchema(e.target.value)} />
            <small className="muted">内建实现只有 echo 与 current_time；注册其它名字不会获得执行能力。</small>
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
      {!tools.length && <p className="muted">还没有注册任何工具。</p>}
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
        {!audits.length && <p className="muted">暂无审计记录。</p>}
      </div>
    </section>
  </div>;
}

export function RunsPanel({agents,onError}:{agents:Agent[];onError:(err:unknown)=>void}) {
  const [runs,setRuns]=useState<Run[]>([]);
  const [agentId,setAgentId]=useState("");
  const [selected,setSelected]=useState<Run|null>(null);
  const [events,setEvents]=useState<RunEvent[]>([]);

  const reload = useCallback(async () => {
    try { setRuns((await api.runs(agentId || undefined,30)).items); }
    catch(err) { onError(err); }
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
      {!runs.length && <p className="muted">还没有运行记录。发一条消息即可产生。</p>}
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

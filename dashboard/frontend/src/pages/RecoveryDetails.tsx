import { useEffect, useState } from "react";
import { backendUrl } from "../lib/backendOrigin";
import "./recovery.css";

type View = { job_id: string; name: string; kind: string; operation_id: string; execution_state: string; business_verdict: string; resumed: boolean; attempts: number; stop_reason: string | null; evidence_at: string; pending_operations: { operation_id: string; state: string; reason: string }[]; budget: { model_calls: number; model_limit: number; tool_calls: number; tool_limit: number; deadline: string }; model_requests: number };
type Detail = { view: View; budget_events: { call_id: string; kind: string; generation: number; created_at: string }[] };
const names: Record<string, string> = { "saved-tool-call": "保存模型响应后恢复", "committed-tool-result": "业务提交后恢复", "concurrent-resume": "重复并发恢复", "storage-unavailable": "状态存储中断后恢复", "tool-budget-exhausted": "工具预算耗尽", "model-reservation-lost": "未返回的模型调用仍计费", "final-model-budget": "业务已确认，Agent 预算停止", "absolute-deadline": "原截止时间已到", "user-abort": "用户中止", "late-result-after-abort": "中止后旧结果返回", "old-worker-return": "旧 worker 返回结果", "insufficient-read-evidence": "读取证据暂时不可用", "gitea-resume": "Gitea Issue 恢复", "gitea-unknown-empty": "Gitea 缺少写入证据" };
const states: Record<string, string> = { completed: "执行完成", waiting_verification: "等待核实", aborted: "用户已中止", budget_stopped: "预算停止", running: "执行中", queued: "等待执行", prepared: "尚未确认发送", sent_unknown: "已发送，结果未知", conflict: "发现冲突" };
const reasons: Record<string, string> = { aborted: "用户中止已生效", model_budget_exhausted: "模型调用次数已用完", tool_budget_exhausted: "工具调用次数已用完", deadline_exhausted: "原任务截止时间已到" };
const time = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });
async function get<T>(path: string): Promise<T> {
  const response = await fetch(backendUrl(`/api/business/recovery${path}`));
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "恢复证据暂不可用");
  return data;
}

export function RecoveryDetails() {
  const [runs, setRuns] = useState<View[]>([]);
  const [selected, setSelected] = useState("");
  const [moment, setMoment] = useState("after-recovery");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let stopped = false;
    setLoading(true); setDetail(null);
    get<{ ready: boolean; runs: View[] }>("/runs").then(data => {
      if (stopped) return;
      setRuns(data.runs); setReady(data.ready); setError("");
      setSelected(current => data.runs.some(r => r.job_id === current) ? current : data.runs[0]?.job_id || "");
    }).catch(e => { if (!stopped) { setRuns([]); setReady(false); setError(e.message); } })
      .finally(() => { if (!stopped) setLoading(false); });
    return () => { stopped = true; };
  }, [reload]);
  useEffect(() => {
    if (!selected || !ready) return;
    let stopped = false;
    setDetail(null);
    get<Detail>(`/runs/${selected}?snapshot=${moment}`).then(data => { if (!stopped) { setDetail(data); setError(""); } })
      .catch(e => { if (!stopped) setError(e.message); });
    return () => { stopped = true; };
  }, [selected, moment, reload, ready]);
  const v = detail?.view;
  return <section className="biz-panel recovery-panel" aria-label="任务恢复详情">
    <div className="biz-section-title"><div><p className="biz-eyebrow">RECOVERY · D30</p><h3>任务恢复详情</h3></div><button aria-label="刷新恢复证据" onClick={() => setReload(n => n + 1)}>↻</button></div>
    <p className="biz-hint">独立复查后的本机验收记录 · 离线模型客户端 · 只读查看。恢复执行与业务确认分别展示。</p>
    {error && <p className="biz-error" role="alert">{error}</p>}
    {loading ? <p className="biz-empty">正在读取恢复证据…</p> : !ready ? <p className="biz-empty">尚无通过独立复查的恢复验收记录。</p> : <>
      <div className="recovery-selects"><label>恢复场景<select aria-label="恢复场景" value={selected} onChange={e => setSelected(e.target.value)}>{runs.map(r => <option key={r.job_id} value={r.job_id}>{names[r.name] || r.name}</option>)}</select></label>
        <label>观察时间点<select aria-label="观察时间点" value={moment} onChange={e => setMoment(e.target.value)}><option value="after-recovery">首次恢复后的证据</option><option value="completed">最终复查的证据</option></select></label></div>
      {!v ? !error && <p className="biz-empty">正在读取所选证据…</p> : <div aria-live="polite">
        <div className="recovery-facts">
          <div><span>执行状态</span><strong>{states[v.execution_state] || v.execution_state}</strong></div>
          <div><span>业务判定</span><strong className={v.business_verdict === "PASS" ? "recovery-confirmed" : "recovery-unknown"}>{v.business_verdict === "PASS" ? "已确认成功" : "证据不足 · 待核实"}</strong></div>
          <div><span>是否恢复过</span><strong>{v.resumed ? "是" : "否"}</strong><small>已领取执行 {v.attempts} 次</small></div>
          <div><span>仍需核实的操作</span><strong>{v.pending_operations.length} 项</strong></div>
        </div>
        {v.stop_reason && <p className="recovery-stop">{reasons[v.stop_reason] || v.stop_reason}。{v.pending_operations.length ? "已中止或停止的执行仍需核实业务结果。" : "已确认的业务结果保留。"}</p>}
        {v.pending_operations.map(op => <div className="recovery-pending" key={op.operation_id}><b>{op.operation_id} · {states[op.state] || op.state}</b><p>{op.reason}</p></div>)}
        <div className="recovery-budgets"><div><label htmlFor="recovery-model-budget">模型调用（离线模拟）<b>{v.budget.model_calls} / {v.budget.model_limit}</b></label><meter id="recovery-model-budget" min={0} max={Math.max(1, v.budget.model_limit)} value={v.budget.model_calls} /></div>
          <div><label htmlFor="recovery-tool-budget">工具 HTTP 调用<b>{v.budget.tool_calls} / {v.budget.tool_limit}</b></label><meter id="recovery-tool-budget" min={0} max={Math.max(1, v.budget.tool_limit)} value={v.budget.tool_calls} /></div></div>
        <p className="biz-hint">原截止时间：{time(v.budget.deadline)} · 证据时间：{time(v.evidence_at)}<br />失败或未返回的调用也占用预算，恢复不会重置次数或延长截止时间。付费模型请求：{v.model_requests} 次。</p>
        <details><summary>查看预算账本 · {detail.budget_events.length} 条记录</summary><ol className="recovery-ledger">{detail.budget_events.map(e => <li key={e.call_id}><b>{e.kind === "model" ? "模型调用预记账" : e.kind === "tool" ? "工具调用预记账" : reasons[e.kind] || e.kind}</b><span>第 {e.generation} 次执行 · {time(e.created_at)}</span></li>)}</ol></details>
        <small className="biz-id">{v.kind} · {v.job_id}</small>
      </div>}
    </>}
  </section>;
}

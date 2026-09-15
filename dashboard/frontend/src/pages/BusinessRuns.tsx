import { useEffect, useState } from "react";
import { backendUrl } from "../lib/backendOrigin";
import "./business.css";
import { BusinessComparison } from "./BusinessComparison";
import { RecoveryDetails } from "./RecoveryDetails";
import { BackendCapabilities, type BusinessFeatures } from "./BackendCapabilities";

type Case = { supported_agents?: string[]; case_id: string; task: string; scenario?: string; fault: { kind?: string } | null; limits: { max_tool_calls: number } };
type Job = { agent?: string; run_id: string; case_id: string; app_version: string; status: string; business_status: string | null; created_at: string; error?: string };
type Check = { check_id: string; expected: unknown; actual: unknown; passed: boolean; evidence: string[] };
type Detail = { job: Job; run: { lifecycle_phase: string; execution_status: string; mode: string; tool_calls: number; client_result: unknown } | null; case: Case | null; checks: { status: string; reason: string; checks: Check[] } | null; initial: unknown; final: unknown; events: { seq: number; event: string; time_utc: string; [key: string]: unknown }[] };

const titles: Record<string, string> = {
  T01: "正常创建", T02: "成功请求重放", T03: "提交后丢响应，再重试", T04: "提交后丢响应，先查询",
  T05: "临时不可用后恢复", T06: "持续不可用，预算内停止", T07: "权限拒绝后停止", T08: "同一操作，请求内容冲突",
  T09: "同一编号的租户隔离", T10: "不同操作的相同描述", T11: "成功响应缺少编号", T12: "同一操作并发两次",
};
const labels: Record<string, string> = { queued: "等待执行", running: "执行中", preparing: "准备环境", observing: "读取最终状态", checking: "独立检查", finished: "已结束", completed: "执行完成", error: "执行错误", timed_out: "已超时", interrupted: "已中断" };
const checkLabels: Record<string, string> = { ticket_count_for_operation: "当前操作的工单数量", customer_id: "客户", device_id: "设备", description: "业务描述", status: "工单状态", returned_ticket_id: "返回编号与数据库一致", client_status: "客户端结果状态", client_reason: "停止原因", unrelated_rows_unchanged: "无关记录保持不变", fault_coverage: "故障确实命中", tool_call_budget: "调用预算", prior_ticket_preserved: "保留原工单", concurrent_same_result: "两次并发返回同一编号", malformed_result_no_fabrication: "缺少编号时不编造结果", queries_match_scoped_database: "查询符合租户和操作范围" };
const terminal = (job: Job) => !["queued", "running"].includes(job.status);
const name = (id: string) => titles[id.slice(0, 3)] || id;
const pretty = (value: unknown) => JSON.stringify(value, null, 2) ?? "—";

async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(backendUrl(`/api/business${path}`), body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    const issues: { type?: string; loc?: (string | number)[]; msg?: string }[] = Array.isArray(data?.detail) ? data.detail : [];
    const oldBackend = issues.some(issue => issue.type === "extra_forbidden" && issue.loc?.join(".") === "body.agent");
    if (oldBackend) throw new Error("当前地址的后端仍是旧版本，不支持“执行方式”（agent）参数。请使用新版服务地址，或重启当前后端后刷新页面；本次运行尚未创建。");
    const explanation = issues.map(issue => `${issue.loc?.filter(part => part !== "body").join(".") || "请求参数"}：${issue.msg || "格式不符合要求"}`).join("；");
    throw new Error(typeof data?.detail === "string" ? data.detail : explanation ? `请求参数不符合要求（${response.status}）：${explanation}` : `请求失败（${response.status}）`);
  }
  return data;
}

export function BusinessRuns() {
  return <div className="biz-page"><BackendCapabilities>{features => <BusinessRunsContent features={features} />}</BackendCapabilities></div>;
}

function BusinessRunsContent({ features }: { features: BusinessFeatures }) {
  const [cases, setCases] = useState<Case[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [caseId, setCaseId] = useState("");
  const [version, setVersion] = useState("fixed");
  const [agent, setAgent] = useState("scripted");
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let stopped = false;
    setLoading(true);
    Promise.all([request<Case[]>("/cases"), request<Job[]>("/runs")]).then(([catalog, history]) => {
      if (stopped) return;
      setCases(catalog); setJobs(history); setError("");
      setCaseId(current => current || catalog.find(c => c.case_id.startsWith("T03"))?.case_id || catalog[0]?.case_id || "");
      setSelected(current => current || history[0]?.run_id || "");
    }).catch(e => { if (!stopped) setError(e.message); }).finally(() => { if (!stopped) setLoading(false); });
    return () => { stopped = true; };
  }, [reload]);

  useEffect(() => {
    if (!selected) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    setDetail(null);
    const poll = async () => {
      try {
        const result = await request<Detail>(`/runs/${selected}`);
        if (stopped) return;
        setDetail(result); setError("");
        setJobs(current => [result.job, ...current.filter(j => j.run_id !== selected)].sort((a, b) => b.created_at.localeCompare(a.created_at)));
        if (!terminal(result.job)) timer = setTimeout(poll, 700);
      } catch (e) { if (!stopped) setError((e as Error).message); }
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [selected, reload]);

  useEffect(() => {
    if (!jobs.some(job => !terminal(job))) return;
    let stopped = false;
    const timer = setTimeout(() => {
      request<Job[]>("/runs").then(history => { if (!stopped) setJobs(history); })
        .catch(e => { if (!stopped) setError(e.message); });
    }, 1000);
    return () => { stopped = true; clearTimeout(timer); };
  }, [jobs]);

  const run = async () => {
    setBusy(true); setError("");
    try {
      const job = await request<Job>("/runs", { case_id: caseId, app_version: version, agent });
      setJobs(current => [job, ...current]); setSelected(job.run_id);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const activeCase = cases.find(c => c.case_id === caseId);
  const badge = (value: string) => <span className={`biz-badge biz-${value.toLowerCase()}`}>{labels[value] || value}</span>;
  const download = (file: string) => backendUrl(`/api/business/runs/${selected}/artifacts/${file}`);
  const available = new Set(["case.json", "run.json", "initial.json", "final.json", "checks.json", "events.jsonl", "report.md"]);

  return <div className="biz-page">
    <div className="biz-heading"><div><p className="biz-eyebrow">BUSINESS VERIFICATION · D11 / D12</p><h2>业务结果验证</h2><p>从真实数据库确认结果，沿事件证据理解每一次判断。</p></div><span className="biz-mode">确定性客户端 · 无模型调用</span></div>
    {error && <div className="biz-error" role="alert">{error}<button onClick={() => setReload(n => n + 1)}>重新加载</button></div>}
    {features.recovery ? <RecoveryDetails /> : <p className="biz-hint">此后端不支持当前恢复证据视图。</p>}
    <section className="biz-panel biz-config" aria-label="运行配置">
      <label>业务案例<select value={caseId} onChange={e => { setCaseId(e.target.value); setAgent("scripted"); }} disabled={loading}>{cases.map(c => <option key={c.case_id} value={c.case_id}>{c.case_id.slice(0, 3)} · {name(c.case_id)}</option>)}</select></label>
      <label>服务版本<select value={version} onChange={e => setVersion(e.target.value)}><option value="fixed">修复版 · 原子幂等</option><option value="unsafe">缺陷版 · 无幂等保护</option></select></label>
      <label>执行方式<select value={agent} onChange={e => setAgent(e.target.value)}><option value="scripted">Scripted · 确定性客户端</option>{activeCase?.supported_agents?.includes("langgraph") && <option value="langgraph">LangGraph · 确定性状态图</option>}</select></label>
      <button className="biz-primary" onClick={run} disabled={busy || !caseId || jobs.some(j => !terminal(j))}>{busy ? "正在提交…" : jobs.some(j => !terminal(j)) ? "运行进行中…" : "运行案例 →"}</button>
      <p className="biz-hint">{activeCase ? `${activeCase.fault?.kind || "无注入故障"} · 工具调用上限 ${activeCase.limits.max_tool_calls} 次 · 每次运行使用独立数据库` : "正在加载案例…"}</p>
    </section>
    {features.comparison ? <BusinessComparison jobs={jobs} checkLabels={checkLabels} /> : <p className="biz-hint">此后端不支持当前比较功能。</p>}
    <div className="biz-layout">
      <aside className="biz-panel biz-history"><div className="biz-section-title"><h3>运行记录</h3><button onClick={() => setReload(n => n + 1)} aria-label="刷新运行记录">↻</button></div>
        {!jobs.length && <p className="biz-empty">{loading ? "加载中…" : "还没有运行。选择一个案例，生成第一份业务报告。"}</p>}
        {jobs.map(job => <button key={job.run_id} className={`biz-run ${selected === job.run_id ? "selected" : ""}`} onClick={() => setSelected(job.run_id)}>
          <div><strong>{job.case_id.slice(0, 3)}</strong>{badge(job.business_status || job.status)}</div><span>{name(job.case_id)}</span><small>{job.app_version === "fixed" ? "修复版" : "缺陷版"} · {job.agent || "scripted"} · {new Date(job.created_at).toLocaleString("zh-CN", { hour12: false })}</small>
        </button>)}
      </aside>
      <article className="biz-detail">
        {!detail ? <div className="biz-panel biz-empty">{selected ? "正在读取运行证据…" : "选择运行记录，查看期望、实际与判定依据。"}</div> : <>
          <section className="biz-panel biz-result" aria-live="polite"><div className="biz-section-title"><h3>{name(detail.job.case_id)}</h3>{badge(detail.job.business_status || detail.job.status)}</div>
            <p>{detail.job.error || detail.checks?.reason || "运行器正在准备或执行案例，完成后自动读取检查结果。"}</p>
            <div className="biz-facts"><span>版本：<b>{detail.job.app_version} / {detail.job.agent || "scripted"}</b></span><span>执行：<b>{labels[detail.job.status] || detail.job.status}</b></span><span>阶段：<b>{labels[detail.run?.lifecycle_phase || detail.job.status] || detail.run?.lifecycle_phase}</b></span><span>调用：<b>{detail.run?.tool_calls ?? "—"} / {detail.case?.limits.max_tool_calls ?? "—"}</b></span></div>
            <small className="biz-id">{detail.job.run_id}</small>
            <p className="biz-hint">PASS 表示符合本案例要求；权限拒绝或等待核实，也可能是正确的业务行为。</p>
          </section>
          {detail.checks && <section className="biz-panel"><div className="biz-section-title"><h3>逐项业务检查</h3><a href={download("report.md")}>下载报告 ↗</a></div>
            <div className="biz-table-wrap"><table><thead><tr><th>检查项</th><th>期望值</th><th>实际值</th><th>证据</th></tr></thead><tbody>{detail.checks.checks.map(check => <tr key={check.check_id} className={check.passed ? "" : "biz-failed-row"}>
              <th><span className={check.passed ? "biz-ok" : "biz-no"}>{check.passed ? "✓" : "✕"}</span> {checkLabels[check.check_id] || check.check_id}</th><td><pre>{pretty(check.expected)}</pre></td><td><pre>{pretty(check.actual)}</pre></td><td>{check.evidence.map(ref => { const file = ref.split(":")[0]; return available.has(file) ? <a key={ref} href={download(file)}>{ref}</a> : <span key={ref}>{ref}（只读观察）</span>; })}</td>
            </tr>)}</tbody></table></div>{!detail.checks.checks.length && <p className="biz-empty">证据不足或执行出错，未生成业务检查项。</p>}
          </section>}
          <section className="biz-panel"><div className="biz-section-title"><h3>事件证据 · {detail.events.length}</h3>{detail.events.length > 0 && <a href={download("events.jsonl")}>下载 JSONL ↗</a>}</div>
            <div className="biz-timeline">{detail.events.map(event => <details key={event.seq}><summary><span>{String(event.seq).padStart(2, "0")}</span><strong>{event.event}</strong><time>{new Date(event.time_utc).toLocaleTimeString("zh-CN", { hour12: false })}</time></summary><pre>{pretty(event)}</pre></details>)}</div>
          </section>
          <section className="biz-panel biz-snapshots"><h3>原始观察与客户端结果</h3>{[["初始数据库", detail.initial], ["最终数据库", detail.final], ["客户端结果", detail.run?.client_result]].map(([title, value]) => <details key={String(title)}><summary>{String(title)}</summary><pre>{pretty(value)}</pre></details>)}</section>
        </>}
      </article>
    </div>
  </div>;
}

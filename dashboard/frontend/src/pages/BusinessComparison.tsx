import { useEffect, useState } from "react";
import { backendUrl } from "../lib/backendOrigin";

type Job = { run_id: string; case_id: string; app_version: string; agent?: string; status: string; business_status: string | null; created_at: string };
type Check = { expected: unknown; actual: unknown; passed: boolean; evidence: string[] };
type Side = { job_id: string; run_id: string; app_version: string; agent: string; status: string; reason: string; tool_calls: number; client_result: unknown };
type Comparison = { controlled: boolean; explanation: string; blockers: string[]; caveat: string; evidence_mode: string; left: Side; right: Side;
  controls: { field: string; left: unknown; right: unknown; same: boolean }[]; checks: { check_id: string; left: Check | null; right: Check | null }[] };
const pretty = (value: unknown) => JSON.stringify(value, null, 2) ?? "缺少检查证据";
const version = (value: string) => value === "fixed" ? "修复版" : "缺陷版";

export function BusinessComparison({ jobs, checkLabels }: { jobs: Job[]; checkLabels: Record<string, string> }) {
  const [left, setLeft] = useState("");
  const [right, setRight] = useState("");
  const [data, setData] = useState<Comparison | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const available = jobs.filter(job => !["queued", "running"].includes(job.status));
  const selectedCase = jobs.find(job => job.run_id === left)?.case_id;
  const candidates = available.filter(job => job.case_id === selectedCase && job.run_id !== left);

  useEffect(() => {
    setData(null); setError("");
    if (!left || !right) { setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true);
    fetch(backendUrl(`/api/business/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`), { signal: controller.signal })
      .then(async response => { const value = await response.json(); if (!response.ok) throw new Error(value.detail || "无法读取对比证据"); return value; })
      .then(value => { if (!controller.signal.aborted) setData(value); })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [left, right, refresh]);

  const option = (job: Job) => `${job.case_id.slice(0, 3)} · ${version(job.app_version)} · ${job.agent || "scripted"} · ${job.business_status || job.status} · ${new Date(job.created_at).toLocaleTimeString("zh-CN")} · ${job.run_id.slice(-6)}`;
  const cell = (check: Check | null, side: Side) => <>
    <b className={check?.passed ? "biz-ok" : "biz-no"}>{check ? check.passed ? "✓ 通过" : "✕ 未通过" : "缺少检查"}</b>
    <p className="biz-hint">期望</p><pre>{pretty(check?.expected)}</pre><p className="biz-hint">实际</p><pre>{pretty(check?.actual)}</pre>
    <a href={backendUrl(`/api/business/runs/${side.job_id}/artifacts/checks.json`)}>原始检查报告 ↗</a>
  </>;

  return <section className="biz-panel biz-comparison" aria-label="运行对比">
    <div className="biz-section-title"><div><h3>同一案例 · 运行对比</h3><p className="biz-hint">先核对改变了什么，再看业务结果如何变化。</p></div><button onClick={() => setRefresh(n => n + 1)} disabled={!left || !right} aria-label="重新读取对比">↻</button></div>
    <div className="biz-pair-selects"><label>左侧运行<select value={left} onChange={e => { setLeft(e.target.value); setRight(""); }}><option value="">选择一次已结束的运行</option>{available.map(job => <option key={job.run_id} value={job.run_id}>{option(job)}</option>)}</select></label>
      <label>右侧运行<select value={right} onChange={e => setRight(e.target.value)} disabled={!left}><option value="">选择同一案例的另一次运行</option>{candidates.map(job => <option key={job.run_id} value={job.run_id}>{option(job)}</option>)}</select></label></div>
    {available.length < 2 && <p className="biz-hint">先运行同一案例的缺陷版和修复版，再选择两份记录。</p>}
    {left && candidates.length === 0 && <p className="biz-hint">这个案例尚无第二份可对比的记录。</p>}
    {loading && <p role="status">正在独立复查两次运行的证据…</p>}
    {error && <p className="biz-error" role="alert">{error}</p>}
    {data && <>
      <div className={`biz-comparison-note ${data.controlled ? "" : "biz-comparison-warning"}`} role="status"><strong>{data.controlled ? "已核对比较条件" : "不能归因于单项修改"}</strong><p>{data.explanation}</p>{data.blockers.map(reason => <p key={reason}>{reason}</p>)}<small>{data.caveat}</small></div>
      <div className="biz-pair-cards">{([data.left, data.right] as Side[]).map((side, index) => <div key={side.job_id}><div className="biz-section-title"><h3>{index === 0 ? "左侧" : "右侧"} · {version(side.app_version)}</h3><span className={`biz-badge biz-${side.status.toLowerCase()}`}>{side.status}</span></div><p>{side.reason}</p><p className="biz-hint">{side.agent} · 工具调用 {side.tool_calls} 次</p><small className="biz-id">{side.run_id}</small><a href={backendUrl(`/api/business/runs/${side.job_id}/artifacts/report.md`)}>下载本次报告 ↗</a></div>)}</div>
      <h4>变化的配置</h4><div className="biz-table-wrap"><table><thead><tr><th>配置</th><th>左侧</th><th>右侧</th></tr></thead><tbody>{data.controls.filter(item => !item.same).map(item => <tr key={item.field}><th>{item.field}</th><td><pre>{pretty(item.left)}</pre></td><td><pre>{pretty(item.right)}</pre></td></tr>)}</tbody></table></div>
      {!data.controls.some(item => !item.same) && <p className="biz-hint">已记录配置无变化。</p>}
      <details><summary>查看一致的配置（{data.controls.filter(item => item.same).length} 项）</summary>{data.controls.filter(item => item.same).map(item => <div key={item.field}><b>{item.field}</b><pre>{pretty(item.left)}</pre></div>)}</details>
      <h4>逐项结果对照</h4><p className="biz-hint">{data.evidence_mode}。下载链接保留原始报告；磁盘证据变化后，以本次复查为准。</p>
      <div className="biz-table-wrap"><table><thead><tr><th>检查项</th><th>左侧结果</th><th>右侧结果</th></tr></thead><tbody>{data.checks.map(row => <tr key={row.check_id} className={row.left?.passed === row.right?.passed ? "" : "biz-failed-row"}><th>{checkLabels[row.check_id] || row.check_id}</th><td>{cell(row.left, data.left)}</td><td>{cell(row.right, data.right)}</td></tr>)}</tbody></table></div>
      <div className="biz-pair-cards">{[data.left, data.right].map(side => <details key={side.job_id}><summary>{version(side.app_version)} · 客户端实际结果</summary><pre>{pretty(side.client_result)}</pre><a href={backendUrl(`/api/business/runs/${side.job_id}/artifacts/events.jsonl`)}>下载本次事件证据 ↗</a></details>)}</div>
    </>}
  </section>;
}

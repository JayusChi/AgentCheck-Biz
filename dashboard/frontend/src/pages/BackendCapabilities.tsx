import { useEffect, useState, type ReactNode } from "react";
import { backendUrl, getBackendOrigin } from "../lib/backendOrigin";

type Capabilities = { backend_version: string; api_version: string; features: Record<string, number> };
export type BusinessFeatures = { comparison: boolean; recovery: boolean };

export function BackendCapabilities({ children }: { children: (features: BusinessFeatures) => ReactNode }) {
  const [data, setData] = useState<Capabilities | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const address = getBackendOrigin() || window.location.origin;
  useEffect(() => {
    const controller = new AbortController();
    let stopped = false;
    let timedOut = false;
    setLoading(true); setData(null); setError("");
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 5000);
    fetch(backendUrl("/api/business/capabilities"), { signal: controller.signal, cache: "no-store" })
      .then(async response => {
        if (!response.ok) throw new Error(response.status === 404 ? "后端未提供版本发现接口，可能仍在运行旧版本。" : `版本发现失败（HTTP ${response.status}）。`);
        const value = await response.json();
        if (typeof value?.backend_version !== "string" || typeof value?.api_version !== "string" || !value?.features || typeof value.features !== "object" || Array.isArray(value.features))
          throw new Error("后端版本响应格式不兼容，无法确认功能支持情况。");
        if (!stopped) setData(value);
      }).catch(e => { if (!stopped) setError(timedOut ? "版本检测超时，无法确认当前后端支持的功能。" : e.message); })
      .finally(() => { clearTimeout(timeout); if (!stopped) setLoading(false); });
    return () => { stopped = true; clearTimeout(timeout); controller.abort(); };
  }, [reload]);
  const compatible = !loading && !error && data?.api_version === "business-api/2";
  const runs = compatible && data?.features.business_runs === 1;
  const message = loading ? "正在检测实际连接的后端…" : error || (!compatible ? "接口版本不兼容；当前页面需要 business-api/2。" : !runs ? "此后端未声明支持当前业务运行功能。" : "版本检测通过，已按后端能力开放功能。");
  return <>
    <section className={`biz-backend ${runs ? "biz-backend-ready" : "biz-backend-blocked"}`} aria-label="后端版本与兼容性" aria-live="polite">
      <div><strong>{data ? `后端 ${data.backend_version} · ${data.api_version}` : "后端版本尚未确认"}</strong><p>{message}</p>
        <p>实际请求地址：<code>{address}</code></p>
        {!runs && !loading && <p>请确认该地址是否为新版服务，重启对应后端或检查页面的后端地址配置，然后重新检测。</p>}
      </div><button onClick={() => setReload(n => n + 1)} disabled={loading}>重新检测</button>
    </section>
    {runs ? children({ comparison: data?.features.comparison === 1, recovery: data?.features.recovery_evidence === 1 }) :
      <section className="biz-panel biz-compatibility-disabled" aria-label="暂不可用的业务功能"><h2>业务结果验证</h2><p>确认版本兼容后即可加载案例、恢复证据和比较记录。</p><button className="biz-primary" disabled>运行案例 →</button></section>}
  </>;
}

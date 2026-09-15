import { useState } from "react";
import type { ComparisonResponse } from "../types";

interface MitigationState {
  retry_backoff: boolean;
  schema_validation: boolean;
  injection_scanner: boolean;
  output_verifier: boolean;
}

const MITIGATION_OPTIONS: {
  key: keyof MitigationState;
  label: string;
  typicallyAddresses: string;
}[] = [
  { key: "retry_backoff", label: "退避重试", typicallyAddresses: "超时、临时 API 错误" },
  { key: "schema_validation", label: "响应结构校验", typicallyAddresses: "响应结构变化" },
  { key: "injection_scanner", label: "注入过滤", typicallyAddresses: "提示词注入、数据外传" },
  { key: "output_verifier", label: "输出校验", typicallyAddresses: "输出格式不一致" },
];

interface MitigationPanelProps {
  comparison: ComparisonResponse;
  onRunMitigation: (mitigation: MitigationState) => Promise<void>;
  onShowTraces: () => void;
  running: boolean;
  disabled?: boolean;
  disabledReason?: string;
}

export function MitigationPanel({
  comparison,
  onRunMitigation,
  onShowTraces,
  running,
  disabled,
  disabledReason,
}: MitigationPanelProps) {
  const [mitigation, setMitigation] = useState<MitigationState>({
    retry_backoff: false,
    schema_validation: false,
    injection_scanner: false,
    output_verifier: false,
  });

  const toggle = (key: keyof MitigationState) =>
    setMitigation((prev) => ({ ...prev, [key]: !prev[key] }));

  const anySelected = Object.values(mitigation).some(Boolean);
  const failedCheckCount = comparison.primary_checks_faulted.filter((check) => !check.passed).length;
  const hadFaultedFailure = failedCheckCount > 0;
  const hasMitigatedRun = comparison.mitigated_trajectory != null;
  const showVerdict = hasMitigatedRun && hadFaultedFailure && !running;
  const verdictPassed = Boolean(comparison.fix_confirmed);

  return (
    <section className="mitigation-panel" style={{ marginTop: "2rem" }}>
      <h3 className="config-card-title">{hadFaultedFailure ? "尝试缓解措施" : "当前没有失败的主要检查项"}</h3>
      <p className="config-card-desc">
        {hadFaultedFailure
          ? `故障执行有 ${failedCheckCount} 项主要检查未通过。选择缓解措施后，使用同一故障重新运行，观察这些检查是否通过。`
          : "故障执行通过了所有主要检查。你仍可测试缓解措施，但当前没有需要修复的失败检查项。"}
      </p>

      <div className="mitigation-toggle-grid" style={{ marginBottom: "1rem" }}>
        {MITIGATION_OPTIONS.map((opt) => {
          const isSelected = mitigation[opt.key];
          return (
            <label
              key={opt.key}
              className={`mitigation-toggle-card ${isSelected ? "selected" : ""}`}
              style={{ position: "relative" }}
            >
              <input
                type="checkbox"
                checked={isSelected}
                onChange={() => toggle(opt.key)}
                className="mitigation-checkbox-hidden"
              />
              <div className="mitigation-toggle-label-row">
                <div className="mitigation-toggle-indicator" />
                <span className="mitigation-toggle-label">{opt.label}</span>
              </div>
              <span className="mitigation-toggle-note">
                适用于： {opt.typicallyAddresses}
              </span>
            </label>
          );
        })}
      </div>

      <div className="mitigation-actions">
        <p className="mitigation-selection-note">
          {anySelected
            ? hadFaultedFailure
              ? "已准备好使用相同故障重新运行。"
              : "已准备好重新运行；当前没有失败的检查项需要修复。"
            : "请至少选择一项缓解措施。"}
        </p>
        <button
          type="button"
          className={`config-run-btn mitigation-run-btn ${anySelected ? "is-active" : "is-inactive"}`}
          disabled={running || !anySelected || disabled}
          onClick={() => void onRunMitigation(mitigation)}
          title={disabled ? disabledReason : undefined}
        >
          {running ? "正在应用缓解措施并重新运行…" : "应用缓解措施并重跑"}
        </button>
      </div>
      {disabled && disabledReason && (
        <p className="config-footer-note" style={{ marginTop: "0.5rem", fontSize: "0.76rem", color: "var(--text-light)" }}>
          {disabledReason}
        </p>
      )}

      {showVerdict && (
        <p
          className={`mitigation-verdict-line ${verdictPassed ? "mitigation-verdict-pass" : "mitigation-verdict-fail"}`}
          role="status"
        >
          {verdictPassed ? (
            <>
              <strong>缓解检查通过：</strong> 应用缓解措施后，之前失败的主要检查项均已通过。{" "}
              <button type="button" className="mitigation-verdict-link" onClick={onShowTraces}>
                查看轨迹
              </button>
            </>
          ) : (
            <>
              <strong>缓解检查未通过：</strong> 应用缓解措施后，仍有至少一项主要检查未通过。{" "}
              <button type="button" className="mitigation-verdict-link" onClick={onShowTraces}>
                查看轨迹
              </button>
            </>
          )}
        </p>
      )}
    </section>
  );
}

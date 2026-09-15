import { useEffect, useRef, useState } from "react";
import { getCheckDescription, formatUiError, getRunLabel } from "../lib/displayText";
import { api } from "../api";
import { MitigationPanel } from "../components/MitigationPanel";
import { ResponseDiff } from "../components/ResponseDiff";
import {
  TrajectoryGraph,
  TrajectoryNodeCard,
  getTrajectoryStepLabel,
} from "../components/TrajectoryGraph";
import type { ComparisonResponse, TrajectoryStepDef } from "../types";
import { getFaultTypeName } from "../lib/faultTypes";

interface ComparisonWorkbenchProps {
  comparison: ComparisonResponse;
  onComparisonUpdate: (updated: ComparisonResponse) => void;
  liveMode: boolean;
}

function formatFaultLabel(faultType: string | undefined): string {
  return getFaultTypeName(faultType);
}

function findInjectionStep(trajectory: TrajectoryStepDef[]): TrajectoryStepDef | undefined {
  return trajectory.find(
    (s) => s.step_type === "tool_response" && s.data.injected_response != null
  );
}

function describeRecoveryAction(action: string | null | undefined): string {
  switch (action) {
    case "recovered":
      return "已恢复";
    case "safe_abort":
      return "安全终止";
    case "propagated":
      return "故障已传播";
    case "crashed":
      return "执行崩溃";
    default:
      return "未见明确恢复";
  }
}

function diagnosticBadgeClass(tone: "pass" | "fail" | "neutral"): string {
  if (tone === "pass") {
    return "badge leg-b-badge pass";
  }
  if (tone === "fail") {
    return "badge leg-b-badge fail";
  }
  return "badge leg-b-badge outcome-badge diagnostic";
}

function recoveryTone(action: string | null | undefined): "pass" | "fail" | "neutral" {
  switch (action) {
    case "recovered":
      return "pass";
    case "propagated":
    case "crashed":
      return "fail";
    default:
      return "neutral";
  }
}

function firstCheckDescription(
  checks: { description: string; passed: boolean }[],
  passed: boolean
): string | null {
  const check = checks.find((check) => check.passed === passed);
  return check ? getCheckDescription(check.description) : null;
}

function describeAlignedRow(
  cleanStep: TrajectoryStepDef | undefined,
  faultedStep: TrajectoryStepDef | undefined,
  rowIndex: number,
  divergenceIndex: number | null
): string {
  if (cleanStep && faultedStep) {
    if (divergenceIndex != null && rowIndex === divergenceIndex) {
      if (
        faultedStep.step_type === "tool_response" &&
        faultedStep.data.injected_response != null
      ) {
        return `第 ${rowIndex + 1} 步：注入后的响应`;
      }
      if (faultedStep.step_type === "llm_generation") {
        return `第 ${rowIndex + 1} 步：推理出现分歧`;
      }
      if (faultedStep.step_type === "final_answer") {
        return `第 ${rowIndex + 1} 步：最终回答出现分歧`;
      }
      return `第 ${rowIndex + 1} 步：${getTrajectoryStepLabel(faultedStep)}出现分歧`;
    }

    if (cleanStep.step_type === faultedStep.step_type) {
      return `第 ${rowIndex + 1} 步：对齐${getTrajectoryStepLabel(faultedStep)}`;
    }
    return `第 ${rowIndex + 1} 步：对齐比较`;
  }

  if (cleanStep) {
    return `第 ${rowIndex + 1} 步：仅正常执行包含此步`;
  }
  if (faultedStep) {
    return `第 ${rowIndex + 1} 步：仅故障执行包含此步`;
  }
  return `第 ${rowIndex + 1} 步`;
}

export function ComparisonWorkbench({
  comparison,
  onComparisonUpdate,
  liveMode,
}: ComparisonWorkbenchProps) {
  const [selectedStep, setSelectedStep] = useState<{
    source: "clean" | "faulted" | "mitigated";
    step: TrajectoryStepDef;
  } | null>(null);
  const [mitigating, setMitigating] = useState(false);
  const [mitigationError, setMitigationError] = useState<string | null>(null);
  const [showTraces, setShowTraces] = useState(false);
  const [showMitigatedTraces, setShowMitigatedTraces] = useState(false);
  const [pendingShowTraces, setPendingShowTraces] = useState(false);
  const mitigatedSectionRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (pendingShowTraces && comparison.mitigated_trajectory) {
      mitigatedSectionRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
      setPendingShowTraces(false);
    }
  }, [pendingShowTraces, comparison.mitigated_trajectory]);

  const handleShowMitigatedTraces = () => {
    setShowMitigatedTraces(true);
    setPendingShowTraces(true);
  };

  const injectionStep = findInjectionStep(comparison.faulted_trajectory);
  const divergenceIndex = comparison.divergence.node_index;
  const pairedRowCount = Math.max(
    comparison.clean_trajectory.length,
    comparison.faulted_trajectory.length
  );

  const primaryChecksFaulted = comparison.primary_checks_faulted;
  const diagnosticsFaulted = comparison.diagnostics_faulted;
  const primaryChecksMitigated = comparison.primary_checks_mitigated;
  const faultChecksPassed = primaryChecksFaulted.every((check: any) => check.passed);
  const failedCheckCount = primaryChecksFaulted.filter((check: any) => !check.passed).length;
  const faultLabel = formatFaultLabel(comparison.fault?.fault_type);
  const faultToolId = comparison.fault?.tool_id ?? comparison.injection_point.tool_id;
  const faultOccurrence = comparison.fault?.occurrence ?? comparison.injection_point.occurrence;
  const firstPassedCheck = firstCheckDescription(primaryChecksFaulted, true);
  const firstFailedCheck = firstCheckDescription(primaryChecksFaulted, false);

  // This banner always describes the ORIGINAL clean-vs-faulted comparison,
  // never the mitigation outcome — that has its own summary next to the
  // mitigated run section below, so the two don't get conflated.
  let summaryTitle = "故障执行通过检查";
  let summaryTone: "is-warning" | "is-stable" = "is-stable";
  let summaryCopy = `注入${faultLabel}后，${
    comparison.divergence.diverged ? "执行轨迹发生变化" : "执行轨迹未发生变化"
  }，故障执行通过了主要检查${
    firstPassedCheck ? `：${firstPassedCheck}` : "。"
  }`;

  if (!faultChecksPassed) {
    summaryTitle = "故障执行未通过检查";
    summaryTone = "is-warning";
    summaryCopy = `注入${faultLabel}后，${
      comparison.divergence.diverged ? "执行轨迹发生变化，" : ""
    }有 ${failedCheckCount} 项主要检查未通过${firstFailedCheck ? `：${firstFailedCheck}` : "。"}`;
  }

  const selectStep = (
    source: "clean" | "faulted" | "mitigated",
    step: TrajectoryStepDef
  ) => setSelectedStep({ source, step });

  const handleRunMitigation = async (mitigation: {
    retry_backoff: boolean;
    schema_validation: boolean;
    injection_scanner: boolean;
    output_verifier: boolean;
  }) => {
    if (!comparison.mcp_server_url || !comparison.model || !comparison.harness || !comparison.task || !comparison.fault) {
      setMitigationError("此对比缺少重新运行所需的配置，无法测试缓解措施。");
      return;
    }
    setMitigating(true);
    setMitigationError(null);
    try {
      const updated = await api.runWorkbench(
        comparison.mcp_server_url,
        comparison.model,
        comparison.harness,
        comparison.task,
        comparison.fault,
        mitigation
      );

      // Preserve the faulted baseline from this comparison; only refresh the mitigated outcome.
      const failedBaselineIds = new Set(
        comparison.primary_checks_faulted.filter((check: any) => !check.passed).map((check: any) => check.check_id)
      );
      const passedMitigatedIds = new Set(
        (updated.primary_checks_mitigated ?? []).filter((check: any) => check.passed).map((check: any) => check.check_id)
      );
      const fixConfirmed =
        failedBaselineIds.size > 0 &&
        [...failedBaselineIds].every((id) => passedMitigatedIds.has(id));

      const merged: ComparisonResponse = {
        ...comparison,
        mitigated_trajectory: updated.mitigated_trajectory,
        mitigated_final_answer: updated.mitigated_final_answer,
        mitigated_run_error: updated.mitigated_run_error,
        primary_checks_mitigated: updated.primary_checks_mitigated,
        diagnostics_mitigated: updated.diagnostics_mitigated,
        fix_confirmed: fixConfirmed,
      };
      onComparisonUpdate(merged);
      if (merged.mitigated_trajectory) {
        setShowMitigatedTraces(false);
        setPendingShowTraces(true);
      }
    } catch (err) {
      setMitigationError(err instanceof Error ? formatUiError(err) : "缓解执行失败。");
    } finally {
      setMitigating(false);
    }
  };

  const handleToggleTraces = () => {
    setShowTraces((value) => {
      const next = !value;
      if (!next) {
        setSelectedStep(null);
      }
      return next;
    });
  };

  return (
    <div className="comparison-workbench">
      <div className="divergence-banner" role="status">
        <div className="divergence-banner-head">
          <span className={`divergence-banner-eyebrow ${summaryTone}`}>
            {summaryTitle}
          </span>
          <span className="divergence-banner-callout">
            正常 {comparison.clean_trajectory.length} 步 / 故障 {comparison.faulted_trajectory.length} 步
          </span>
        </div>
        <p className="divergence-banner-copy">{summaryCopy}</p>
        <div className="divergence-summary-badges">
          <span className={`badge ${faultChecksPassed ? "pass" : "fail"}`}>
            {faultChecksPassed ? "主要检查通过" : "主要检查未通过"}
          </span>
          {diagnosticsFaulted && (
            <>
              <span className={diagnosticBadgeClass(recoveryTone(diagnosticsFaulted.recovery_action))}>
                恢复情况： {describeRecoveryAction(diagnosticsFaulted.recovery_action)}
              </span>
            </>
          )}
        </div>
        {comparison.fault_spec && (
          <div className="divergence-fault-line">
            <span className="divergence-fault-label">注入的故障</span>
            <code>{faultLabel}</code>
            <span className="divergence-fault-label">目标工具</span>
            <code>{faultToolId}</code>
            <span className="divergence-fault-label">第 {faultOccurrence} 次调用</span>
          </div>
        )}
      </div>

      <div className="leg-checks-panel comparison-surface">
        <h3 className="comparison-column-title">主要通过 / 失败检查</h3>
        <p className="config-card-desc">
          使用确定性规则检查智能体是否正确处理了注入的故障。
        </p>
        {primaryChecksFaulted.length === 0 ? (
          <p className="config-card-desc leg-empty-state">
            此故障类型没有适用的主要检查项。
          </p>
        ) : (
          <ul className="leg-a-list">
            {primaryChecksFaulted.map((check: any) => (
              <li key={check.check_id} className={check.passed ? "leg-a-pass" : "leg-a-fail"}>
                <span className="leg-a-icon">{check.passed ? "\u2713" : "\u2717"}</span>
                <span title={check.description}>{getCheckDescription(check.description)}</span>
              </li>
            ))}
          </ul>
        )}

        {diagnosticsFaulted && (
          <div className="leg-b-panel">
            <h4 className="leg-b-title">诊断标签</h4>
            <p className="config-card-desc">
              由大语言模型评审的标签，概括故障识别、恢复行为和不确定性表达。
            </p>
            <div className="leg-b-badges">
              <span className={diagnosticBadgeClass(diagnosticsFaulted.failure_detected ? "pass" : "neutral")}>
                是否说明问题： {diagnosticsFaulted.failure_detected ? "是" : "否"}
              </span>
              <span className={diagnosticBadgeClass(recoveryTone(diagnosticsFaulted.recovery_action))}>
                恢复情况： {describeRecoveryAction(diagnosticsFaulted.recovery_action)}
              </span>
              <span
                className={diagnosticBadgeClass(diagnosticsFaulted.uncertainty_communicated ? "pass" : "neutral")}
              >
                是否表达不确定性： {diagnosticsFaulted.uncertainty_communicated ? "是" : "否"}
              </span>
            </div>
          </div>
        )}
      </div>

      <div className="comparison-surface trace-toggle-panel">
        <div className="trace-toggle-header">
          <div>
            <h3 className="comparison-column-title">执行轨迹对比</h3>
            <p className="config-card-desc">
              展开后可逐步对比正常与故障执行。任务、工具响应和模型输出保留原文，便于核对实验数据。
            </p>
          </div>
          <button type="button" className="config-inline-action" onClick={handleToggleTraces}>
            {showTraces ? "收起轨迹" : "查看轨迹"}
          </button>
        </div>
      </div>

      {showTraces && (
        <>
          <div className="comparison-columns comparison-columns-aligned">
            <div className="comparison-columns-header">
              <div className="comparison-column comparison-column-clean comparison-column-shell">
                <h3 className="comparison-column-title">正常执行（Clean run）</h3>
                <p className="comparison-column-subtitle">
                  基线执行 · {comparison.clean_trajectory.length} 步
                </p>
                {comparison.clean_run_error && (
                  <p className="run-error-notice">执行失败：{formatUiError(comparison.clean_run_error)}</p>
                )}
              </div>
              <div className="comparison-column comparison-column-faulted comparison-column-shell">
                <h3 className="comparison-column-title">故障执行（Faulted run）</h3>
                <p className="comparison-column-subtitle">
                  已注入故障 · {comparison.faulted_trajectory.length} 步
                </p>
                {comparison.faulted_run_error && (
                  <p className="run-error-notice">执行失败：{formatUiError(comparison.faulted_run_error)}</p>
                )}
              </div>
            </div>
            {Array.from({ length: pairedRowCount }).map((_, rowIndex) => {
              const cleanStep = comparison.clean_trajectory[rowIndex];
              const faultedStep = comparison.faulted_trajectory[rowIndex];

              return (
                <div className="comparison-step-row-wrap" key={`row-${rowIndex}`}>
                  <div className="comparison-step-row-label">
                    {describeAlignedRow(cleanStep, faultedStep, rowIndex, divergenceIndex)}
                  </div>
                  <div className="comparison-step-row">
                    <div
                      className={`comparison-step-cell ${rowIndex > 0 ? "has-previous" : ""}`}
                    >
                      {cleanStep ? (
                        <TrajectoryNodeCard
                          step={cleanStep}
                          variant="clean"
                          isSelected={
                            selectedStep?.source === "clean" &&
                            selectedStep.step.index === cleanStep.index
                          }
                          onSelectStep={(step) => selectStep("clean", step)}
                        />
                      ) : (
                        <div className="trajectory-node-spacer" aria-hidden="true" />
                      )}
                    </div>
                    <div
                      className={`comparison-step-cell ${rowIndex > 0 ? "has-previous" : ""}`}
                    >
                      {faultedStep ? (
                        <TrajectoryNodeCard
                          step={faultedStep}
                          variant="faulted"
                          isDivergenceNode={divergenceIndex != null && rowIndex === divergenceIndex}
                          isSelected={
                            selectedStep?.source === "faulted" &&
                            selectedStep.step.index === faultedStep.index
                          }
                          onSelectStep={(step) => selectStep("faulted", step)}
                        />
                      ) : (
                        <div className="trajectory-node-spacer" aria-hidden="true" />
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {injectionStep && (
            <div className="injection-diff-panel comparison-surface">
              <h3 className="comparison-column-title">正常响应与故障注入后的响应</h3>
              <ResponseDiff
                clean={
                  (typeof injectionStep.data.clean_response === "object"
                    ? injectionStep.data.clean_response
                    : { value: injectionStep.data.clean_response }) as Record<string, unknown>
                }
                injected={
                  (typeof injectionStep.data.injected_response === "object"
                    ? injectionStep.data.injected_response
                    : { value: injectionStep.data.injected_response }) as Record<string, unknown>
                }
              />
            </div>
          )}

          {selectedStep?.step && (
            <div className="selected-step-panel comparison-surface">
              <div className="selected-step-header">
                <h4 className="selected-step-title">
                  步骤详情（{getRunLabel(selectedStep.source)} · {getTrajectoryStepLabel(selectedStep.step)}）
                </h4>
                <button
                  type="button"
                  className="kv-remove-btn"
                  aria-label="关闭步骤详情"
                  onClick={() => setSelectedStep(null)}
                >
                  &times;
                </button>
              </div>
              <pre className="response-box selected-step-code">
                {JSON.stringify(selectedStep.step.data, null, 2)}
              </pre>
            </div>
          )}

        </>
      )}

      <MitigationPanel
        comparison={comparison}
        onRunMitigation={handleRunMitigation}
        onShowTraces={handleShowMitigatedTraces}
        running={mitigating}
        disabled={!liveMode || !comparison.mcp_server_url}
        disabledReason={
          !liveMode
            ? "请切换到“连接 MCP 服务器”并运行实时对比，再测试缓解措施。"
            : undefined
        }
      />
      {mitigationError && (
        <p className="upload-errors" role="alert">
          {mitigationError}
        </p>
      )}

      {comparison.mitigated_trajectory && (
        <div
          className="comparison-column comparison-column-mitigated comparison-surface"
          style={{ marginTop: "1.5rem" }}
          ref={mitigatedSectionRef}
        >
          <h3 className="comparison-column-title">缓解执行结果（Mitigated run）</h3>
          {primaryChecksMitigated && (
            <ul className="leg-a-list" style={{ marginBottom: "1rem" }}>
              {primaryChecksMitigated.map((check: any) => (
                <li key={check.check_id} className={check.passed ? "leg-a-pass" : "leg-a-fail"}>
                  <span className="leg-a-icon">{check.passed ? "\u2713" : "\u2717"}</span>
                  <span title={check.description}>{getCheckDescription(check.description)}</span>
                </li>
              ))}
            </ul>
          )}

          <div className="trace-toggle-header">
            <div>
              <h3 className="comparison-column-title">缓解执行轨迹</h3>
              <p className="config-card-desc">
                查看应用缓解措施后的逐步轨迹 · {comparison.mitigated_trajectory.length} 步
              </p>
            </div>
            <button
              type="button"
              className="config-inline-action"
              onClick={() =>
                setShowMitigatedTraces((value) => {
                  const next = !value;
                  if (!next && selectedStep?.source === "mitigated") {
                    setSelectedStep(null);
                  }
                  return next;
                })
              }
            >
              {showMitigatedTraces ? "收起缓解轨迹" : "查看缓解轨迹"}
            </button>
          </div>

          {showMitigatedTraces && (
            <>
              <TrajectoryGraph
                trajectory={comparison.mitigated_trajectory}
                variant="mitigated"
                onSelectStep={(step) => selectStep("mitigated", step)}
                selectedIndex={
                  selectedStep?.source === "mitigated"
                    ? comparison.mitigated_trajectory.findIndex(
                        (step) => step.index === selectedStep.step.index
                      )
                    : null
                }
              />
              {selectedStep?.step && selectedStep.source === "mitigated" && (
                <div className="selected-step-panel" style={{ marginTop: "1rem" }}>
                  <div className="selected-step-header">
                    <h4 className="selected-step-title">
                      步骤详情（{getRunLabel(selectedStep.source)} · {getTrajectoryStepLabel(selectedStep.step)}）
                    </h4>
                    <button
                      type="button"
                      className="kv-remove-btn"
                      aria-label="关闭步骤详情"
                      onClick={() => setSelectedStep(null)}
                    >
                      &times;
                    </button>
                  </div>
                  <pre className="response-box selected-step-code">
                    {JSON.stringify(selectedStep.step.data, null, 2)}
                  </pre>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

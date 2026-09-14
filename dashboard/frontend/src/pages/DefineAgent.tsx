import { useCallback, useEffect, useMemo, useState } from "react";
import { formatUiError } from "../lib/displayText";
import { api } from "../api";
import { DEMO_MCP_TOOLS, DemoMcpToolsDialog } from "../components/DemoMcpToolsDialog";
import { TargetToolField } from "../components/TargetToolField";
import { FaultTypesDialog } from "../components/FaultTypesDialog";
import { TaskGuidanceDialog } from "../components/TaskGuidanceDialog";
import { ReadFirstDialog } from "../components/ReadFirstDialog";
import { ComparisonWorkbench } from "./ComparisonWorkbench";
import { FAULT_TYPES, getFaultTypeName } from "../lib/faultTypes";
import { backendUrl, getBackendOrigin } from "../lib/backendOrigin";
import { DEFAULT_DEMO_TASK } from "../lib/taskGuidance";
import type { ComparisonResponse, ExampleSummary } from "../types";

const MODEL_OPTIONS = [
  { value: "qwen3.7-max", label: "通义千问 3.7 Max（阿里云百炼）" },
  { value: "qwen3.8-max", label: "通义千问 3.8 Max（阿里云百炼）" },
  { value: "qwen3.7-max-2026-06-08", label: "通义千问 3.7 Max · 2026-06-08（阿里云百炼）" },
  { value: "google/gemini-2.5-flash", label: "Gemini 2.5 Flash" },
  { value: "deepseek-v4-pro", label: "DeepSeek V4 Pro" },
  { value: "meta-llama/llama-3.3-70b-instruct", label: "Llama 3.3 70B" },
  { value: "gpt-4.1-mini", label: "GPT-4.1 mini" },
];

function defaultDemoMcpUrl(): string {
  const origin = getBackendOrigin();
  return origin ? backendUrl("/mcp") : "";
}

export function DefineAgent() {
  const [examples, setExamples] = useState<ExampleSummary[]>([]);
  const [mode, setMode] = useState<"connect" | "explore">("connect");
  const [mcpSource, setMcpSource] = useState<"builtin" | "custom">("builtin");
  const [mcpServerUrl, setMcpServerUrl] = useState(defaultDemoMcpUrl);
  const [model, setModel] = useState("qwen3.7-max");
  const [harness, setHarness] = useState<"react" | "native_tool_calling">("native_tool_calling");
  const [task, setTask] = useState(
    "Open incident brief-11 and explain what caused the outage and whether it is still active."
  );
  const [targetToolId, setTargetToolId] = useState("get_incident_brief");
  const [injectionOccurrence, setInjectionOccurrence] = useState(1);
  const [faultType, setFaultType] = useState("A1");
  const [exampleSearch, setExampleSearch] = useState("");
  const [selectedExampleId, setSelectedExampleId] = useState<string | null>(null);

  const [running, setRunning] = useState(false);
  const [loadingExample, setLoadingExample] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [comparison, setComparison] = useState<ComparisonResponse | null>(null);
  const [toolsDialogOpen, setToolsDialogOpen] = useState(false);
  const [faultTypesDialogOpen, setFaultTypesDialogOpen] = useState(false);
  const [taskGuidanceDialogOpen, setTaskGuidanceDialogOpen] = useState(false);
  const [readFirstOpen, setReadFirstOpen] = useState(false);
  const [taskJustApplied, setTaskJustApplied] = useState(false);

  const applyDefaultTask = useCallback(() => {
    setMode("connect");
    setMcpSource("builtin");
    setMcpServerUrl(defaultDemoMcpUrl());
    setTask(DEFAULT_DEMO_TASK.task);
    setTargetToolId(DEFAULT_DEMO_TASK.targetToolId ?? "get_incident_brief");
    setFaultType("A1");
    setInjectionOccurrence(1);
    setSelectedExampleId(null);
    setComparison(null);
    setTaskJustApplied(true);
    requestAnimationFrame(() => {
      document.getElementById("task")?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }, []);

  useEffect(() => {
    if (!taskJustApplied) return;
    const timer = window.setTimeout(() => setTaskJustApplied(false), 2200);
    return () => window.clearTimeout(timer);
  }, [taskJustApplied]);

  useEffect(() => {
    api.listExamples()
      .then(setExamples)
      .catch((err) => setError(err instanceof Error ? formatUiError(err) : "加载配置数据失败。"));
  }, []);

  const filteredExamples = useMemo(() => {
    const q = exampleSearch.trim().toLowerCase();
    if (!q) return examples.slice(0, 60);
    return examples.filter(
      (ex) =>
        ex.example_id.toLowerCase().includes(q) ||
        ex.fault_type.toLowerCase().includes(q) ||
        getFaultTypeName(ex.fault_type).toLowerCase().includes(q) ||
        ex.task.toLowerCase().includes(q)
    );
  }, [examples, exampleSearch]);

  const builtinMcpUrl = useMemo(() => defaultDemoMcpUrl(), []);
  const effectiveMcpUrl = mcpSource === "builtin" ? builtinMcpUrl : mcpServerUrl;

  const handleMcpSourceChange = (source: "builtin" | "custom") => {
    setMcpSource(source);
    if (source === "builtin") {
      setMcpServerUrl(defaultDemoMcpUrl());
      if (!DEMO_MCP_TOOLS.some((tool) => tool.value === targetToolId)) {
        setTargetToolId("get_incident_brief");
      }
    }
  };

  const handleRun = async () => {
    setError(null);
    setComparison(null);
    setMode("connect");
    if (!effectiveMcpUrl.trim() || !task.trim() || !targetToolId.trim()) {
      setError("请填写 MCP 服务器地址、任务内容和故障注入目标工具。");
      return;
    }

    setRunning(true);
    try {
      const result = await api.runWorkbench(effectiveMcpUrl, model, harness, task, {
        fault_type: faultType,
        tool_id: targetToolId.trim(),
        occurrence: injectionOccurrence,
      });
      setComparison(result);
    } catch (err) {
      setError(err instanceof Error ? formatUiError(err) : "对比执行失败。");
    } finally {
      setRunning(false);
    }
  };

  const loadExample = async (exampleId: string) => {
    setError(null);
    setLoadingExample(true);
    setSelectedExampleId(exampleId);
    setMode("explore");
    try {
      const result = await api.exampleComparison(exampleId);
      setComparison(result);
    } catch (err) {
      setError(err instanceof Error ? formatUiError(err) : "加载案例失败。");
    } finally {
      setLoadingExample(false);
    }
  };

  return (
    <div className="config-page">
      <div className="config-header">
        <span className="eyebrow">AgentCheck</span>
        <h2>复现与调试 MCP 智能体故障</h2>
        {/* <p>
          Compare clean and faulted trajectories, inspect injected response diffs, review primary
          pass/fail checks, and re-run with mitigations.
        </p> */}
      </div>

      <button type="button" className="read-first-banner" onClick={() => setReadFirstOpen(true)}>
        <span className="read-first-banner-icon" aria-hidden="true">
          i
        </span>
        <span className="read-first-banner-text">
          <strong>首次使用请先阅读</strong>，快速了解演示操作。
        </span>
        <span className="read-first-banner-cta">查看</span>
      </button>

      <div className="config-tabs">
        <button
          type="button"
          className={`config-inline-action ${mode === "connect" ? "active" : ""}`}
          onClick={() => setMode("connect")}
        >
          连接 MCP 服务器
        </button>
        <button
          type="button"
          className={`config-inline-action ${mode === "explore" ? "active" : ""}`}
          onClick={() => setMode("explore")}
        >
          浏览预置案例
        </button>
      </div>

      <div className="config-grid">
        <section className="config-panel-left">
          <div className="config-panel-scroll">
            {mode === "connect" ? (
              <>
                <h3 className="config-card-title">运行配置</h3>
                <p className="config-card-desc" style={{ marginBottom: "1rem" }}>
                  使用内置演示 MCP 或连接自己的服务器，然后选择模型、执行方式、任务、故障类型、目标工具和注入次数。
                </p>

                <aside className="injection-prerequisite" role="note">
                  <span className="injection-prerequisite-icon" aria-hidden="true">
                    !
                  </span>
                  <p className="injection-prerequisite-text">
                    <strong>请选择任务中会用到的工具。</strong> 只有智能体实际调用该工具时，故障注入才会生效。任务未使用该工具时，不会注入故障。
                  </p>
                </aside>

                <div className="form-field">
                  <label>MCP 服务器</label>
                  <div className="config-tabs mcp-source-tabs">
                    <button
                      type="button"
                      className={`config-inline-action ${mcpSource === "builtin" ? "active" : ""}`}
                      onClick={() => handleMcpSourceChange("builtin")}
                    >
                      内置演示 MCP
                    </button>
                    <button
                      type="button"
                      className={`config-inline-action ${mcpSource === "custom" ? "active" : ""}`}
                      onClick={() => handleMcpSourceChange("custom")}
                    >
                      自定义 MCP 服务器
                    </button>
                  </div>
                </div>

                {mcpSource === "builtin" ? (
                  <p className="config-footer-note mcp-source-hint">
                    点击{" "}
                    <button
                      type="button"
                      className="inline-text-link"
                      onClick={() => setToolsDialogOpen(true)}
                    >
                      这里
                    </button>{" "}
                    查看可用工具。
                  </p>
                ) : (
                  <div className="form-field">
                    <label htmlFor="mcp-server-url">MCP 服务器地址</label>
                    <input
                      id="mcp-server-url"
                      className="field-input"
                      value={mcpServerUrl}
                      onChange={(e) => setMcpServerUrl(e.target.value)}
                      placeholder="https://your-server.example/mcp"
                    />
                  </div>
                )}

                <div className="config-row config-row-two">
                  <div className="form-field">
                    <label htmlFor="model">模型</label>
                    <select
                      id="model"
                      className="field-select"
                      value={model}
                      onChange={(e) => setModel(e.target.value)}
                    >
                      {MODEL_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="form-field">
                    <label htmlFor="harness">执行方式</label>
                    <select
                      id="harness"
                      className="field-select"
                      value={harness}
                      onChange={(e) => setHarness(e.target.value as "react" | "native_tool_calling")}
                    >
                      <option value="react">ReAct</option>
                      <option value="native_tool_calling">原生工具调用（Native tool calling）</option>
                    </select>
                  </div>
                </div>

                <div className="form-field">
                  <label htmlFor="task">任务</label>
                  <textarea
                    id="task"
                    className={`field-input field-textarea ${taskJustApplied ? "field-flash" : ""}`}
                    rows={4}
                    value={task}
                    onChange={(e) => setTask(e.target.value)}
                    placeholder={
                      mcpSource === "builtin"
                        ? "描述智能体需要使用演示工具查找或报告什么…"
                        : "描述智能体需要使用你的 MCP 工具完成什么目标…"
                    }
                  />
                  <p className="config-footer-note">
                    点击{" "}
                    <button
                      type="button"
                      className="inline-text-link"
                      onClick={() => setTaskGuidanceDialogOpen(true)}
                    >
                      这里
                    </button>{" "}
                    {mcpSource === "builtin"
                      ? "查看示例任务及其对应的演示工具。"
                      : "查看如何为自己的 MCP 服务器编写任务。"}
                  </p>
                </div>

                <div className="config-row">
                  <div className="form-field">
                    <label htmlFor="fault-type">故障类型</label>
                    <select
                      id="fault-type"
                      className="field-select"
                      value={faultType}
                      onChange={(e) => setFaultType(e.target.value)}
                    >
                      {FAULT_TYPES.map((fault) => (
                        <option key={fault.value} value={fault.value}>
                          {fault.name}
                        </option>
                      ))}
                    </select>
                    <p className="config-footer-note">
                      点击{" "}
                      <button
                        type="button"
                        className="inline-text-link"
                        onClick={() => setFaultTypesDialogOpen(true)}
                      >
                        这里
                      </button>{" "}
                      查看各类故障的含义。
                    </p>
                  </div>
                  <TargetToolField
                    value={targetToolId}
                    onChange={setTargetToolId}
                    choices={mcpSource === "builtin" ? DEMO_MCP_TOOLS : undefined}
                    placeholder="search_docs"
                    helperText={
                      mcpSource === "builtin" ? (
                        <>
                          点击{" "}
                          <button
                            type="button"
                            className="inline-text-link"
                            onClick={() => setToolsDialogOpen(true)}
                          >
                            这里
                          </button>{" "}
                          查看每个工具的用途。
                        </>
                      ) : (
                        "请填写 MCP 服务器提供的准确工具名。"
                      )
                    }
                  />
                  <div className="form-field">
                    <label htmlFor="occurrence">在第几次调用时注入</label>
                    <input
                      id="occurrence"
                      type="number"
                      min={1}
                      className="field-input"
                      value={injectionOccurrence}
                      onChange={(e) => setInjectionOccurrence(Math.max(1, parseInt(e.target.value, 10) || 1))}
                    />
                    <p className="config-footer-note">
                      仅对指定的这一次调用注入故障（1 表示首次调用）。
                    </p>
                  </div>
                </div>
              </>
            ) : (
              <>
                <h3 className="config-card-title">预置案例</h3>
                <p className="config-card-desc" style={{ marginBottom: "1rem" }}>
                  查看上游预计算案例；任务、工具响应和模型输出保留原文。
                </p>
                <input
                  className="field-input"
                  placeholder="按案例 ID、故障类型或任务搜索…"
                  value={exampleSearch}
                  onChange={(e) => setExampleSearch(e.target.value)}
                />
                <div className="example-grid" style={{ marginTop: "1rem" }}>
                  {filteredExamples.map((example) => {
                    const isSelected = selectedExampleId === example.example_id;
                    const isLoading = loadingExample && isSelected;
                    return (
                    <div
                      key={example.example_id}
                      role="button"
                      tabIndex={0}
                      className={`example-card ${isSelected ? "selected" : ""}`}
                      onClick={() => void loadExample(example.example_id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          void loadExample(example.example_id);
                        }
                      }}
                      aria-busy={isLoading}
                    >
                      <div className="example-card-meta">
                        <div className="example-card-topline">
                          <span className="example-card-fault-pill">{getFaultTypeName(example.fault_type)}</span>
                        </div>
                        <span className="example-card-title">{example.task}</span>
                        <span className="example-card-id">{example.example_id}</span>
                      </div>
                      <div className="example-card-footer">
                        <span className="example-card-hint">{example.model}</span>
                        <span className="example-card-status">{isLoading ? "加载中…" : "查看"}</span>
                      </div>
                    </div>
                    );
                  })}
                </div>
              </>
            )}
          </div>

          <div className="config-panel-footer">
            {mode === "connect" && (
              <button
                type="button"
                className={`primary config-run-btn ${running ? "is-running" : ""}`}
                disabled={running || !effectiveMcpUrl.trim()}
                onClick={() => void handleRun()}
              >
                {running ? "正在执行对比…" : "运行对比"}
              </button>
            )}
            {error && (
              <p className="upload-errors" role="alert">
                {error}
              </p>
            )}
          </div>
        </section>

        <section className="config-panel-right">
          {mode === "explore" && comparison && (
            <span className="precomputed-badge precomputed-badge-standalone">上游预计算示例 · 非本机实时调用</span>
          )}
          {running && (
            <p className="empty-state">正在进行正常执行与故障执行…</p>
          )}
          {!running && !comparison && (
            <div className="workbench-placeholder">
              <div className="workbench-placeholder-head">
                <h4>准备开始对比</h4>
                <p>
                  在左侧配置运行或选择预置案例，在这里查看执行轨迹与结果。
                </p>
              </div>
              <div className="workbench-placeholder-grid">
                <div className="workbench-placeholder-column">
                  <span className="workbench-placeholder-label">正常执行将显示在这里</span>
                  <div className="workbench-placeholder-node" />
                  <div className="workbench-placeholder-node" />
                  <div className="workbench-placeholder-node" />
                </div>
                <div className="workbench-placeholder-column">
                  <span className="workbench-placeholder-label">故障执行将显示在这里</span>
                  <div className="workbench-placeholder-node" />
                  <div className="workbench-placeholder-node is-faulted" />
                  <div className="workbench-placeholder-node" />
                </div>
              </div>
            </div>
          )}
          {comparison && !running && (
            <ComparisonWorkbench
              comparison={comparison}
              onComparisonUpdate={setComparison}
              liveMode={mode === "connect"}
            />
          )}
        </section>
      </div>

      <DemoMcpToolsDialog open={toolsDialogOpen} onClose={() => setToolsDialogOpen(false)} />
      <FaultTypesDialog open={faultTypesDialogOpen} onClose={() => setFaultTypesDialogOpen(false)} />
      <TaskGuidanceDialog
        open={taskGuidanceDialogOpen}
        mcpSource={mcpSource}
        onClose={() => setTaskGuidanceDialogOpen(false)}
        onSelectExample={(example) => {
          setTask(example.task);
          if (example.targetToolId) {
            setTargetToolId(example.targetToolId);
          }
        }}
      />
      <ReadFirstDialog
        open={readFirstOpen}
        onClose={() => setReadFirstOpen(false)}
        onSelectDefault={applyDefaultTask}
      />
    </div>
  );
}

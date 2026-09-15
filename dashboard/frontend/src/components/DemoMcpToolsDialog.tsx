import { useEffect, useRef } from "react";

export interface DemoMcpTool {
  value: string;
  name: string;
  description: string;
}

export const DEMO_MCP_TOOLS: DemoMcpTool[] = [
  {
    value: "search_docs",
    name: "搜索文档",
    description: "搜索事件与运维文档，查找相关简报的 ID。",
  },
  {
    value: "get_incident_brief",
    name: "获取事件简报",
    description: "根据 doc_id 返回完整简报，包括摘要、根因、状态和时间线。",
  },
  {
    value: "fetch_meta",
    name: "获取元数据",
    description: "返回文档元数据，例如负责人、优先级、服务和解决时间。",
  },
];

interface DemoMcpToolsDialogProps {
  open: boolean;
  onClose: () => void;
}

export function DemoMcpToolsDialog({ open, onClose }: DemoMcpToolsDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
    }
    if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      className="tools-dialog"
      onClose={onClose}
      onClick={(event) => {
        if (event.target === dialogRef.current) {
          onClose();
        }
      }}
    >
      <div className="tools-dialog-panel">
        <header className="tools-dialog-header">
          <div>
            <h2 className="tools-dialog-title">演示 MCP 工具</h2>
            <p className="tools-dialog-subtitle">内置事件简报服务器提供的工具。</p>
          </div>
          <button type="button" className="tools-dialog-close" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </header>
        <ul className="tools-dialog-list">
          {DEMO_MCP_TOOLS.map((tool) => (
            <li key={tool.value} className="tools-dialog-item">
              <div className="tools-dialog-item-head">
                <span className="tools-dialog-item-name">{tool.name}</span>
                <code className="tools-dialog-item-id">{tool.value}</code>
              </div>
              <p className="tools-dialog-item-desc">{tool.description}</p>
            </li>
          ))}
        </ul>
      </div>
    </dialog>
  );
}

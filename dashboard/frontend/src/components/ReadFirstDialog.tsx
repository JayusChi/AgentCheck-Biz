import { useEffect, useRef } from "react";
import { DEFAULT_DEMO_TASK } from "../lib/taskGuidance";

interface ReadFirstDialogProps {
  open: boolean;
  onClose: () => void;
  onSelectDefault: () => void;
}

export function ReadFirstDialog({ open, onClose, onSelectDefault }: ReadFirstDialogProps) {
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

  const handleSelectDefault = () => {
    onSelectDefault();
    onClose();
  };

  return (
    <dialog
      ref={dialogRef}
      className="tools-dialog tools-dialog-wide read-first-dialog"
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
            <h2 className="tools-dialog-title">开始演示前</h2>
            <p className="tools-dialog-subtitle">先选一个合适的任务，即可开始。</p>
          </div>
          <button type="button" className="tools-dialog-close" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </header>
        <div className="read-first-dialog-body">
          <p className="read-first-lead">
            建议先使用默认任务或列表中的示例任务，快速了解对比流程。
          </p>
          <p className="read-first-inline-note" role="note">
            请选择完成任务时需要调用的工具，否则故障不会被注入。
          </p>
          <div className="read-first-task-preview">
            <span className="read-first-task-label">默认任务（原文）</span>
            <p className="read-first-task-text">{DEFAULT_DEMO_TASK.task}</p>
            <span className="read-first-task-tool">
              故障仅注入到 <code>{DEFAULT_DEMO_TASK.targetToolId}</code>
            </span>
          </div>
          <p className="read-first-note">
            熟悉流程后，可以尝试其他示例任务，或连接自己的 MCP 服务器。
          </p>
          <div className="read-first-actions">
            <button type="button" className="primary read-first-use-btn" onClick={handleSelectDefault}>
              载入默认任务
            </button>
            <button type="button" className="read-first-skip-btn" onClick={onClose}>
              自行选择任务
            </button>
          </div>
        </div>
      </div>
    </dialog>
  );
}

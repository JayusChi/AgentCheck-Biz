import { useEffect, useRef } from "react";
import {
  CUSTOM_TASK_EXAMPLE,
  CUSTOM_TASK_TIPS,
  DEMO_TASK_EXAMPLES,
  type TaskExample,
} from "../lib/taskGuidance";

interface TaskGuidanceDialogProps {
  open: boolean;
  mcpSource: "builtin" | "custom";
  onClose: () => void;
  onSelectExample: (example: TaskExample) => void;
}

function TaskExampleRow({
  example,
  onSelectExample,
  onClose,
}: {
  example: TaskExample;
  onSelectExample: (example: TaskExample) => void;
  onClose: () => void;
}) {
  return (
    <li className="tools-dialog-item task-guidance-item">
      <div className="tools-dialog-item-head">
        <span className="tools-dialog-item-name">{example.title}</span>
        <button
          type="button"
          className="task-guidance-use-btn"
          onClick={() => {
            onSelectExample(example);
            onClose();
          }}
        >
          使用此任务
        </button>
      </div>
      <p className="tools-dialog-item-desc">{example.task}</p>
    </li>
  );
}

export function TaskGuidanceDialog({ open, mcpSource, onClose, onSelectExample }: TaskGuidanceDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const isBuiltin = mcpSource === "builtin";

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
      className="tools-dialog tools-dialog-wide"
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
            <h2 className="tools-dialog-title">如何编写任务</h2>
            <p className="tools-dialog-subtitle">
              {isBuiltin
                ? "任务用于说明智能体需要使用演示服务器的事件简报工具完成什么。"
                : "任务用于说明智能体需要使用你的 MCP 工具完成什么目标。"}
            </p>
          </div>
          <button type="button" className="tools-dialog-close" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </header>
        <div className="tools-dialog-list">
          {isBuiltin ? (
            <>
              <p className="task-guidance-note">
                演示服务器内置一份文档：<strong>brief-11</strong>（用户接入故障简报 11）。可选择下面的任务，或直接编辑任务输入框。示例任务保留英文原文。
              </p>
              <ul className="fault-types-section-list">
                {DEMO_TASK_EXAMPLES.map((example) => (
                  <TaskExampleRow
                    key={example.title}
                    example={example}
                    onSelectExample={onSelectExample}
                    onClose={onClose}
                  />
                ))}
              </ul>
            </>
          ) : (
            <>
              <ul className="task-guidance-tips">
                {CUSTOM_TASK_TIPS.map((tip) => (
                  <li key={tip}>{tip}</li>
                ))}
              </ul>
              <ul className="fault-types-section-list">
                <TaskExampleRow
                  example={CUSTOM_TASK_EXAMPLE}
                  onSelectExample={onSelectExample}
                  onClose={onClose}
                />
              </ul>
            </>
          )}
        </div>
      </div>
    </dialog>
  );
}

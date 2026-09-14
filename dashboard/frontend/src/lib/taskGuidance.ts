export interface TaskExample {
  title: string;
  task: string;
  /** Demo MCP tool to inject into when this example is selected. */
  targetToolId?: string;
}

export const DEMO_TASK_EXAMPLES: TaskExample[] = [
  {
    title: "已知文档 ID，读取完整简报",
    task: "Open incident brief-11 and explain what caused the outage and whether it is still active.",
    targetToolId: "get_incident_brief",
  },
  {
    title: "未知文档 ID，先搜索文档",
    task: "Search incident docs for 'malformed cache key'. Tell me which brief matches and quote its title.",
    targetToolId: "search_docs",
  },
  {
    title: "仅查询元数据字段",
    task: "For brief-11, return only the owner team, priority level, and resolved-at timestamp.",
    targetToolId: "fetch_meta",
  },
];

export const DEFAULT_DEMO_TASK = DEMO_TASK_EXAMPLES[0];

export const CUSTOM_TASK_TIPS = [
  "用清晰的语言描述目标：智能体应该查明什么，或返回什么？",
  "如果已知工具需要的 ID、名称或筛选条件，请在任务中写明。",
  "说明合格回答应包含哪些内容，以便判断任务是否完成。",
];

export const CUSTOM_TASK_EXAMPLE: TaskExample = {
  title: "通用示例",
  task: "Use the connected MCP tools to answer: what is the current status of order #48291?",
};

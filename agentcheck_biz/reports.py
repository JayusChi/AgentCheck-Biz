"""JSON evidence plus a readable local report; no model-based judge."""

import json
from pathlib import Path
from uuid import uuid4
import time


def save_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Windows readers may briefly hold a sharing lock while the UI polls.
        for attempt in range(8):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        if temporary.exists():
            temporary.unlink()


def save_check_report(run_dir: Path, result: dict) -> None:
    save_json(run_dir / "checks.json", result)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    lines = [f"# {result['status']}", "", result["reason"], "",
             f"模式：{run['mode']}。", "",
             "证据：[运行](run.json) · [案例](case.json) · [初始状态](initial.json) · "
             "[事件](events.jsonl) · [检查](checks.json)", "",
             "| 检查 | 期望 | 实际 | 通过 |", "|---|---|---|---|"]
    for item in result["checks"]:
        def cell(value):
            return json.dumps(value, ensure_ascii=False).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['check_id']} | {cell(item['expected'])} | "
                     f"{cell(item['actual'])} | {'是' if item['passed'] else '否'} |")
    if run.get("agent") == "llm":
        lines += ["", "模型证据：[配置与用量](model.json) · [请求、响应与工具轨迹](trajectory.json)",
                  "", f"已收到真实模型响应：{run.get('model_called', False)}。"]
    if run.get("plugin"):
        lines += ["", "版本化观察：[初始观察](observation-initial.json) · [最终观察](observation-final.json)",
                  "", f"插件：{run['plugin']['name']} / {run['plugin']['version']}；清理：{run.get('cleanup_status')}。"]
    if run.get("http_service"):
        lines += ["", "HTTP 证据：[服务身份](http-ready.json) · [健康与版本](http-probes.json) · "
                  "[服务端接收与写入](http-service-events.jsonl) · [平台只读观察](http-observer.jsonl) · "
                  "[子进程清理](http-cleanup.json)", "",
                  f"请求尝试：{run.get('http_request_attempts', 0)}；业务请求："
                  f"{run.get('http_business_request_attempts', 0)}；HTTP 客户端自动重试：0。"]
    if run.get("object_type") == "gitea-issue":
        lines += ["", "Gitea 证据：[隔离仓库与版本](gitea-target.json) · [只读 API 请求](gitea-observer.jsonl) · "
                  "[执行请求](gitea-execution.jsonl) · [清理](gitea-cleanup.json)", "",
                  "证据等级：独立只读 API 观察。离线复查核对保存的 API 响应，不表示当前服务器状态或数据库提交事件。"]
    if run.get("transport") == "mcp-stdio":
        lines += ["", f"MCP：SDK {run.get('mcp_sdk_version')}；实际协议 {run.get('mcp_protocol_version')}；stdio。"]
        for name in run.get("mcp_connections", []):
            lines += ["", f"{name}：[握手与工具]({name}-session.json) · [JSON-RPC 往返]({name}-wire.jsonl) · "
                      f"[服务端调用]({name}-server.jsonl) · [子进程清理]({name}-cleanup.json)"]
    if run.get("proxy_enabled"):
        lines += ["", "网络代理：[规则](proxy-rule.json) · [事件](proxy-events.jsonl) · [触发统计](proxy-summary.json) · "
                  "[目标绑定](proxy-target.json) · [清理](proxy-cleanup.json)", "", "故障覆盖与业务成功分别判定；未命中规则不能算作故障验收通过。"]
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

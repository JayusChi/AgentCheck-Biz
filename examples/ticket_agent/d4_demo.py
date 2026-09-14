"""Run A/B/C or one D4 phase, retaining the unsafe FAIL as expected evidence."""

import argparse
from pathlib import Path
from uuid import uuid4

from agentcheck_biz.reports import save_json
from agentcheck_biz.runner import REPO_ROOT, run_ticket_case


PHASES = {"A": ("unsafe", False), "B": ("unsafe", True), "C": ("fixed", True)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=list(PHASES), help="One run, with business verdict exit code")
    args = parser.parse_args()
    output_root = REPO_ROOT / "artifacts" / ("d4-comparison-" + uuid4().hex)
    output_root.mkdir(parents=True, exist_ok=False)
    phases = [args.phase] if args.phase else list(PHASES)
    runs = {}
    lines = ["# D4 正常、故障与修复对照", "",
             "受控客户端；未调用模型；模拟工具边界响应丢失。每阶段独立数据库。", "",
             "A→B 只开启故障；B→C 只更换为服务端幂等版本。请求、初始数据、重试策略与预算相同。", "",
             "每个数据库另有 1 条相同的无关初始工单，下表只统计本次业务操作。", "",
             "| 阶段 | 服务版本 | F1 | 调用次数 | 本次工单数 | 业务结论 | 报告 |",
             "|---|---|---|---|---|---|---|"]
    for phase in phases:
        version, inject = PHASES[phase]
        outcome = run_ticket_case(output_root, app_version=version, inject_fault=inject)
        result, run = outcome["result"], outcome["run"]
        count = next((item["actual"] for item in result["checks"]
                      if item["check_id"] == "ticket_count_for_operation"), "unknown")
        runs[phase] = {"run_id": run["run_id"], "status": result["status"],
                       "ticket_count_for_operation": count, "tool_calls": run["tool_calls"]}
        print(f"[{phase}] app={version}, fault={inject}, tool_calls={run['tool_calls']}, "
              f"operation_tickets={count}, verdict={result['status']}")
        print(f"  {result['reason']}")
        lines.append(f"| {phase} | {version} | {inject} | {run['tool_calls']} | {count} | "
                     f"{result['status']} | [查看]({run['run_id']}/report.md) |")
    expected = {"A": "PASS", "B": "FAIL", "C": "PASS"}
    reproduced = all(runs[phase]["status"] == expected[phase] for phase in phases)
    save_json(output_root / "comparison.json", {"runs": runs, "expected_statuses": expected,
                                                "experiment_reproduced": reproduced})
    lines += ["", "B 的 FAIL 是缺陷版本的业务失败证据。整组实验的成功表示预期对照被复现，不能将 B 算作业务通过。",
              "", f"预期对照已复现：{reproduced}", "",
              "当前范围：创建工单与 F1；没有真实模型、HTTP 故障或完整通用运行器。"]
    report_path = output_root / "comparison.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report: {report_path}")
    if args.phase:
        return outcome["result"]["exit_code"]
    if any(item["status"] == "ERROR" for item in runs.values()):
        return 3
    if any(item["status"] == "INCONCLUSIVE" for item in runs.values()):
        return 2
    return 0 if reproduced else 1


if __name__ == "__main__":
    raise SystemExit(main())

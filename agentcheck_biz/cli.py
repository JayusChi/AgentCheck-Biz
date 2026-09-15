"""Source-checkout CLI for validated local business cases and evidence."""

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from .cases import CaseValidationError, load_case, load_suite
from .checks import EXIT_CODES, load_json
from .verifiers.ticket_local import recheck_ticket_run as check_run
from .reports import save_json
from .runner import REPO_ROOT, run_ticket_case
from examples.ticket_agent.llm_agent import ModelConfig


class CLIParser(argparse.ArgumentParser):
    def error(self, message):
        raise CaseValidationError(message)


def make_parser():
    parser = CLIParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=CLIParser)
    validate = commands.add_parser("validate", help="Validate inputs without creating a run")
    source = validate.add_mutually_exclusive_group(required=True)
    source.add_argument("--case", type=Path)
    source.add_argument("--cases", type=Path)
    run = commands.add_parser("run", help="Run one case; only --agent llm contacts a model")
    run.add_argument("--case", type=Path, required=True)
    run.add_argument("--agent", choices=["scripted", "langgraph", "llm"], default="scripted")
    run.add_argument("--model", help="Only valid with --agent llm")
    run.add_argument("--app-version", choices=["unsafe", "fixed"], default="fixed")
    run.add_argument("--output", type=Path, default=REPO_ROOT / "artifacts")
    suite = commands.add_parser("suite", help="Run every validated JSON case in a directory, serially")
    suite.add_argument("--cases", type=Path, required=True)
    suite.add_argument("--agent", choices=["scripted", "langgraph"], default="scripted")
    suite.add_argument("--app-version", choices=["unsafe", "fixed"], default="fixed")
    suite.add_argument("--output", type=Path, default=REPO_ROOT / "artifacts")
    for name, description in (("status", "Read saved execution state"), ("check", "Recheck saved evidence read-only")):
        command = commands.add_parser(name, help=description)
        command.add_argument("--run-dir", type=Path, required=True)
    compare = commands.add_parser("compare", help="Read-only paired business reports")
    compare.add_argument("--left", type=Path, required=True)
    compare.add_argument("--right", type=Path, required=True)
    return parser


def aggregate_status(statuses, *, interrupted=False):
    if "ERROR" in statuses:
        return "ERROR"
    if interrupted or "INCONCLUSIVE" in statuses:
        return "INCONCLUSIVE"
    return "FAIL" if "FAIL" in statuses else "PASS"


def run_suite(cases, output_root: Path, app_version: str, agent: str = "scripted"):
    if agent == "langgraph":
        from examples.ticket_agent.langgraph_agent import validate_support
        for _, case in cases:
            validate_support(case)
    suite_dir = output_root.resolve() / ("suite-" + uuid4().hex)
    suite_dir.mkdir(parents=True, exist_ok=False)
    summary = {"suite_dir": str(suite_dir), "execution_status": "running", "status": "INCONCLUSIVE",
               "planned_cases": len(cases), "app_version": app_version, "agent": agent, "runs": []}
    save_json(suite_dir / "suite.json", summary)
    interrupted = False
    for source, case in cases:
        entry = {"case_id": case["case_id"], "source": str(source.resolve())}
        try:
            outcome = run_ticket_case(suite_dir, case=case, app_version=app_version,
                                      inject_fault=case["fault"] is not None, agent=agent)
            entry.update(run_dir=outcome["run_dir"], status=outcome["result"]["status"],
                         reason=outcome["result"]["reason"])
            interrupted = outcome["run"]["execution_status"] == "interrupted"
        except KeyboardInterrupt:
            interrupted = True
            entry.update(run_dir=None, status="INCONCLUSIVE", reason="Case interrupted before finalization")
        except Exception as error:
            entry.update(run_dir=None, status="ERROR", reason=f"{type(error).__name__}: {error}")
        summary["runs"].append(entry)
        save_json(suite_dir / "suite.json", summary)
        if interrupted:
            break
    statuses = [run["status"] for run in summary["runs"]]
    summary.update(execution_status="interrupted" if interrupted else "completed",
                   status=aggregate_status(statuses, interrupted=interrupted),
                   run_count=len(statuses), not_started=len(cases) - len(statuses),
                   counts={status: statuses.count(status) for status in EXIT_CODES})
    summary["exit_code"] = EXIT_CODES[summary["status"]]
    save_json(suite_dir / "suite.json", summary)
    lines = [f"# Suite: {summary['status']}", "", f"执行方式：{agent}；无模型调用；每个案例独立环境。", "",
             "| 案例 | 结论 | 原因 | 报告 |", "|---|---|---|---|"]
    for run in summary["runs"]:
        report = f"[查看]({Path(run['run_dir']).name}/report.md)" if run["run_dir"] else "无完整运行目录"
        reason = run["reason"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {run['case_id']} | {run['status']} | {reason} | {report} |")
    lines += ["", f"全部结论计数：{summary['counts']}；未执行：{summary['not_started']}。",
              "", "套件不将缺陷版本的 FAIL 转成 PASS，不过滤 ERROR 或 INCONCLUSIVE。"]
    (suite_dir / "suite.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def execute(args):
    if args.command == "compare":
        from .comparison import compare_runs
        result = compare_runs(args.left, args.right)
        return result, 0 if result["controlled"] else 2
    if args.command == "validate":
        cases = [(args.case, load_case(args.case))] if args.case else load_suite(args.cases)
        return {"status": "PASS", "scope": "configuration_only",
                "case_ids": [case["case_id"] for _, case in cases]}, 0
    if args.command == "run":
        case = load_case(args.case)
        if args.model and args.agent != "llm":
            raise CaseValidationError("--model requires --agent llm")
        config = None
        if args.agent == "llm":
            from dotenv import load_dotenv
            load_dotenv(REPO_ROOT / ".env", override=False)
            limits = case["limits"]
            config = ModelConfig(
                model=args.model or os.getenv("AGENTCHECK_BIZ_MODEL", "qwen3.7-max"),
                max_model_calls=limits.get("max_model_calls", 5),
                max_output_tokens=limits.get("max_output_tokens_per_model_call", 512),
                timeout_seconds=limits.get("model_loop_timeout_seconds", 90),
                request_timeout_seconds=limits.get("request_timeout_seconds", 30),
            )
        outcome = run_ticket_case(args.output, case=case, app_version=args.app_version,
                                  inject_fault=case["fault"] is not None, agent=args.agent, model_config=config)
        return {"status": outcome["result"]["status"], "reason": outcome["result"]["reason"],
                "run_dir": outcome["run_dir"], "execution_status": outcome["run"]["execution_status"],
                "tool_calls": outcome["run"]["tool_calls"]}, outcome["result"]["exit_code"]
    if args.command == "suite":
        cases = load_suite(args.cases)  # Validate ALL inputs before allocating any run.
        result = run_suite(cases, args.output, args.app_version, args.agent)
        return result, result["exit_code"]
    if args.command == "check":
        result = check_run(args.run_dir)
        return result, result["exit_code"]
    if args.command == "status":
        run = load_json(args.run_dir / "run.json")
        execution = run["execution_status"]
        status = (run.get("business_status", "INCONCLUSIVE") if execution == "completed"
                  else "ERROR" if execution in {"error", "timed_out"} else "INCONCLUSIVE")
        return {"status": status, "run_id": run["run_id"], "execution_status": execution,
                "lifecycle_phase": run.get("lifecycle_phase"), "business_status": run.get("business_status")}, EXIT_CODES[status]
    raise CaseValidationError("Unsupported command")


def main(argv=None):
    try:
        args = make_parser().parse_args(argv)
        output, exit_code = execute(args)
    except (CaseValidationError, OSError, ValueError, KeyError) as error:
        output, exit_code = {"status": "ERROR", "error": str(error)}, 3
    except KeyboardInterrupt:
        output, exit_code = {"status": "INCONCLUSIVE", "error": "Command interrupted"}, 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

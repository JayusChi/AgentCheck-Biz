"""D13 fixed nine-slot live experiment. Default: preview only, no credentials."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from .cases import validate_case
from .checks import load_json, observe_database
from .verifiers.ticket_local import recheck_ticket_run as check_run
from .reports import save_json
from .runner import REPO_ROOT, implementation_digest
from examples.ticket_agent.llm_agent import LiveTicketAgent, ModelConfig, SYSTEM_PROMPT, TOOLS


ENDPOINT = "https://llm-jbs3di4fsqypcf49.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
STATUSES = ("PASS", "FAIL", "INCONCLUSIVE", "ERROR", "NOT_STARTED")
TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def make_plan():
    slots = []
    for kind, filename in (("F1", "T03.json"), ("F2", "T05.json"), ("F3", "T07.json")):
        base = load_json(REPO_ROOT / "cases/tickets/core" / filename)
        for phase, version in (("A", "unsafe"), ("B", "unsafe"), ("C", "fixed")):
            case = deepcopy(base)
            if phase == "A":
                case["fault"] = None
                case["expected"].update(ticket_count_for_operation=1, client_status="completed")
                case["expected"].pop("client_reason", None)
            case["limits"].update(max_model_calls=5, max_output_tokens_per_model_call=512,
                                  model_loop_timeout_seconds=90, request_timeout_seconds=30)
            validate_case(case)
            slots.append({"slot_id": f"{kind}-{phase}-1", "fault_family": kind, "phase": phase,
                          "repeat": 1, "app_version": version, "case": case, "case_sha256": digest(case),
                          "initial_state_sha256": digest(case["initial_tickets"])})
    return {"schema_version": 1, "model": "qwen3.7-max", "endpoint": ENDPOINT,
            "planned_runs": 9, "planned_fault_runs": 6, "maximum_model_requests": 45,
            "per_run_limits": {"max_model_calls": 5, "max_tool_calls": 6, "max_output_tokens": 512,
                               "loop_timeout_seconds": 90, "request_timeout_seconds": 30,
                               "parent_timeout_seconds": 115, "temperature": 0, "sdk_retries": 0},
            "stop_policy": "Stop after first ERROR or INCONCLUSIVE; never retry or replace a slot",
            "billing": "Request/output limits only; total input tokens and monetary cost are not capped here",
            "data": "Synthetic repair ticket fields C001/D001/无法开机, generated ticket IDs, scoped local tool results",
            "system_prompt": SYSTEM_PROMPT, "tools": TOOLS,
            "system_prompt_sha256": LiveTicketAgent(ModelConfig()).metadata["system_prompt_sha256"],
            "tools_sha256": LiveTicketAgent(ModelConfig()).metadata["tools_sha256"],
            "slots": slots}


def read_optional(path, default):
    try:
        return load_json(path)
    except (OSError, ValueError):
        return default


def inspect_slot(slot, directory, *, parent_error=None):
    """Derive verdict from SQLite/checker, and partial usage from individual responses."""
    runs = list(directory.glob("biz-*/run.json"))
    row = {key: slot[key] for key in ("slot_id", "fault_family", "phase", "app_version")}
    row.update(status="ERROR", reason=parent_error or "Missing or ambiguous run evidence", run_dir=None,
               request_attempts=0, known_usage={key: 0 for key in TOKEN_KEYS}, unknown_usage_requests=0,
               fault_covered=False, observation_available=False, duplicate=False,
               structured_completion=False, false_completion=None, tool_calls=0, elapsed_seconds=None,
               failed_checks=[])
    if len(runs) != 1:
        row["request_count_uncertain"] = True
        return row
    run_dir = runs[0].parent
    run = read_optional(runs[0], {})
    model = read_optional(run_dir / "model.json", {})
    trace = read_optional(run_dir / "trajectory.json", {"steps": []})
    steps = trace.get("steps", [])
    attempts = max(model.get("model_request_attempts", 0), len(steps))
    known = [step["usage"] for step in steps if isinstance(step.get("usage"), dict)
             and all(type(step["usage"].get(key)) is int and step["usage"][key] >= 0 for key in TOKEN_KEYS)]
    result = check_run(run_dir)
    if read_optional(run_dir / "case.json", None) != slot["case"] or run.get("app_version") != slot["app_version"]:
        parent_error = "Saved case or service version differs from planned slot"
    row.update(status="ERROR" if parent_error else result["status"], reason=parent_error or result["reason"],
               run_dir=str(run_dir), request_attempts=attempts,
               known_usage={key: sum(item[key] for item in known) for key in TOKEN_KEYS},
               unknown_usage_requests=max(0, attempts - len(known)),
               request_count_uncertain=bool(parent_error) or not model,
               tool_calls=run.get("tool_calls", 0), elapsed_seconds=model.get("elapsed_seconds"),
               response_models=sorted({step["response_model"] for step in steps if "response_model" in step}),
               controls={key: model.get(key) for key in ("model", "temperature", "system_prompt_sha256", "tools_sha256",
                                                        "sdk_version", "sdk_retries", "max_model_calls", "max_output_tokens")},
               implementation_sha256=run.get("implementation_sha256"),
               failed_checks=[item["check_id"] for item in result["checks"] if not item["passed"]],
               fault_covered=any(item["check_id"] == "fault_coverage" and item["passed"] for item in result["checks"]))
    client = run.get("client_result") or {}
    row["structured_completion"] = client.get("status") == "completed"
    try:
        observed = observe_database(run_dir / "business.sqlite")
        if observed["run_id"] != run.get("run_id") or run.get("run_id") != run_dir.name:
            raise ValueError("Database identity mismatch")
        matching = [ticket for ticket in observed["tickets"]
                    if all(ticket[key] == value for key, value in slot["case"]["context"].items())]
        row.update(observation_available=True, ticket_count=len(matching), duplicate=len(matching) > 1)
        wanted = slot["case"]["expected"]
        supported = (wanted["ticket_count_for_operation"] == 1 and len(matching) == 1
                     and matching[0]["ticket_id"] == client.get("ticket_id")
                     and all(matching[0][key] == wanted[key] for key in ("customer_id", "device_id", "description", "status")))
        if row["structured_completion"]:
            row["false_completion"] = not supported
    except (OSError, ValueError, KeyError, sqlite3.Error):
        pass
    events = []
    try:
        events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError):
        pass
    row["tool_sequence"] = [event["tool"] for event in events if event.get("event") == "tool_called"]
    row["dedup_hits"] = sum(event.get("event") == "dedup_confirmed" for event in events)
    row["event_trace_complete"] = bool(events) and events[-1].get("event") == "run_finished"
    return row


def summarize(plan, rows):
    counts = {status: sum(row["status"] == status for row in rows) for status in STATUSES}
    observed = [row for row in rows if row.get("observation_available")]
    completed = [row for row in rows if row.get("structured_completion")]
    completion_observed = [row for row in completed if row.get("false_completion") is not None]
    return {"planned_runs": plan["planned_runs"], "started_runs": len(rows) - counts["NOT_STARTED"],
            "counts": counts, "all_slots_executed": counts["NOT_STARTED"] == 0,
            "fault_coverage": {"covered": sum(bool(row.get("fault_covered")) for row in rows),
                               "planned": plan["planned_fault_runs"]},
            "duplicates": {"runs": sum(bool(row.get("duplicate")) for row in observed),
                           "observable_runs": len(observed),
                           "started_without_observation": len(rows) - counts["NOT_STARTED"] - len(observed)},
            "structured_false_completion": {"runs": sum(row["false_completion"] for row in completion_observed),
                                            "observable_completed_claims": len(completion_observed),
                                            "unobserved_completed_claims": len(completed) - len(completion_observed)},
            "request_attempts_recorded": sum(row.get("request_attempts", 0) for row in rows),
            "request_count_uncertain_runs": sum(bool(row.get("request_count_uncertain")) for row in rows),
            "known_usage": {key: sum(row.get("known_usage", {}).get(key, 0) for row in rows) for key in TOKEN_KEYS},
            "unknown_usage_requests": sum(row.get("unknown_usage_requests", 0) for row in rows),
            "tool_calls": sum(row.get("tool_calls", 0) for row in rows),
            "runs": rows,
            "limitations": ["One repeat per cell, fixed order, no statistical/general reliability claim",
                            "A/B changes fault and the F3 oracle; B/C changes service implementation",
                            "False completion checks structured status/ID and database only, not free-text semantics",
                            "Unknown token usage is not zero; provider billing is authoritative",
                            "F1 query recovery does not exercise create retry or prove deduplication"]}


def save_summary(directory, plan, rows, stop_reason=None):
    report = summarize(plan, rows)
    report["stop_reason"] = stop_reason
    save_json(directory / "summary.json", report)
    lines = ["# D13 真实模型实验", "", f"计划 {report['planned_runs']} 次；已启动 {report['started_runs']} 次。",
             f"全部状态：{report['counts']}。故障覆盖：{report['fault_coverage']}。", "",
             "| 样本 | 状态 | 工单数 | 请求数 | 已知 tokens | 工具路径 | 原因 / 原始证据 |",
             "|---|---|---:|---:|---:|---|---|"]
    for row in rows:
        evidence = Path(row["run_dir"]).relative_to(directory).as_posix() + "/report.md" if row.get("run_dir") else None
        reason = row.get("reason", "未执行").replace("|", "\\|").replace("\n", " ")
        link = f"[报告]({evidence})" if evidence else "无报告"
        lines.append(f"| {row['slot_id']} | {row['status']} | {row.get('ticket_count', '未知')} | "
                     f"{row.get('request_attempts', 0)} | {row.get('known_usage', {}).get('total_tokens', 0)} | "
                     f"{' → '.join(row.get('tool_sequence', []))} | {reason} {link} |")
    lines += ["", f"停止原因：{stop_reason or '无'}", "", "```json",
              json.dumps({key: report[key] for key in ("duplicates", "structured_false_completion", "known_usage", "unknown_usage_requests")}, ensure_ascii=False, indent=2),
              "```", "", *[f"- {item}" for item in report["limitations"]]]
    (directory / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def execute_plan(directory, plan, *, launch=None):
    # No arbitrary edited plan, larger budget or alternate payload can enter this entrypoint.
    if plan != make_plan():
        raise ValueError("Plan differs from the fixed reviewed experiment")
    rows = [{"slot_id": slot["slot_id"], "status": "NOT_STARTED"} for slot in plan["slots"]]
    reserved = 0
    source_digest = implementation_digest()
    stop_reason = None
    save_summary(directory, plan, rows)
    for index, slot in enumerate(plan["slots"]):
        if implementation_digest() != source_digest:
            stop_reason = "Implementation changed during experiment"
            break
        if reserved + 5 > plan["maximum_model_requests"]:
            stop_reason = "Request reservation budget exhausted"
            break
        reserved += 5  # Reserve the full cap even on timeout or unknown usage; never recycle it.
        rows[index].update(status="INCONCLUSIVE", reason="Started; result pending")
        save_json(directory / "progress.json", {"reserved_requests": reserved, "active_slot": slot["slot_id"]})
        save_summary(directory, plan, rows)
        slot_dir = directory / slot["slot_id"]
        slot_dir.mkdir()
        save_json(slot_dir / "input.json", slot["case"])
        command = [sys.executable, "-X", "utf8", "-m", "agentcheck_biz.cli", "run", "--agent", "llm",
                   "--model", plan["model"], "--app-version", slot["app_version"],
                   "--case", str(slot_dir / "input.json"), "--output", str(slot_dir)]
        error = None
        print(f"[{index + 1}/9] {slot['slot_id']} starting (reserved {reserved}/45)", flush=True)
        try:
            if launch is not None:  # Offline tests replace the process, never the live verdict.
                launch(slot, slot_dir)
            else:
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
                with (slot_dir / "process.log").open("wb") as log:
                    result = subprocess.run(command, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT,
                                            timeout=115, **options)
                if result.returncode not in (0, 1, 2, 3):
                    error = f"Worker exited unexpectedly: {result.returncode}"
        except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt) as exc:
            error = f"Parent stopped worker: {type(exc).__name__}"
        rows[index] = inspect_slot(slot, slot_dir, parent_error=error)
        print(f"{slot['slot_id']}: {rows[index]['status']}; requests={rows[index]['request_attempts']}", flush=True)
        if rows[index]["status"] in {"ERROR", "INCONCLUSIVE"}:
            stop_reason = rows[index]["reason"]
        save_summary(directory, plan, rows, stop_reason)
        if stop_reason:
            break
    report = save_summary(directory, plan, rows, stop_reason)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly send the nine-slot paid model experiment")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "artifacts")
    args = parser.parse_args()
    plan = make_plan()
    directory = args.output.resolve() / ("d13-live-" if args.live else "d13-preview-")
    directory = directory.with_name(directory.name + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    save_json(directory / "plan.json", plan)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True)
    save_json(directory / "provenance.json", {"started_at": datetime.now(timezone.utc).isoformat(),
              "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
              "implementation_sha256": implementation_digest(), "plan_sha256": digest(plan),
              "working_tree_note": "Includes uncommitted local extension; commit alone does not identify source"})
    print(f"Experiment: {directory}", flush=True)
    if not args.live:
        print("Preview only: no credentials loaded and no model requests sent.")
        return 0
    from dotenv import load_dotenv
    from pipeline.bailian import bailian_connection
    load_dotenv(REPO_ROOT / ".env", override=False)
    _, endpoint = bailian_connection()
    if endpoint.rstrip("/") != ENDPOINT:
        raise ValueError("Configured endpoint differs from the preview; no request sent")
    started = time.monotonic()
    report = execute_plan(directory, plan)
    save_json(directory / "execution.json", {"elapsed_seconds": round(time.monotonic() - started, 3),
              "finished_at": datetime.now(timezone.utc).isoformat()})
    print(json.dumps({"counts": report["counts"], "report": str(directory / "summary.md")}, ensure_ascii=False))
    return 3 if report["stop_reason"] else (1 if report["counts"]["FAIL"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())

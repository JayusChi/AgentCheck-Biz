"""A predeclared 100-slot experiment. Failures remain in their original slots."""

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from uuid import uuid4

from agentcheck_biz.checks import load_json
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.reports import save_json
from agentcheck_biz.recovery.ticket_http import run_case as recovery_case
from agentcheck_biz.service_crash.runner import run_case as crash_case
from examples.business_mcp.commit_loss_demo import ticket_case, gitea_case
from examples.business_mcp.proxy_demo import run_proxy_case
from examples.gitea_target.demo import target_environment
from examples.gitea_target.runtime import GiteaRuntime, DEFAULT_INSTANCE_ROOT
from .profiles import profiles, plan, aggregate, support_matrix
from .verify import hashes, inspect_run, rows
from .process import launch


def execute(profile, output):
    if profile["kind"] == "recovery":
        return recovery_case(output, max_tool_calls=profile["maximum"])
    if profile["kind"] == "crash":
        return crash_case(output, profile["point"], max_tool_calls=profile["maximum"])

    def proxy_case():
        if profile["scenario"] == "commit_loss":
            return (gitea_case(output) if profile["target"] == "gitea"
                    else ticket_case(output, profile["version"], profile["fault"]))
        scenario = load_json(REPO_ROOT / "cases/proxy" / (profile["scenario"] + ".json"))
        if scenario["rule"]:
            scenario["rule"]["tool"] = "create_issue" if profile["target"] == "gitea" else "create_ticket"
        return run_proxy_case(output, plugin_id=profile["target"] + "-mcp",
            case=load_json(REPO_ROOT / ("cases/gitea/G01.json" if profile["target"] == "gitea" else "cases/tickets/full/T01.json")),
            proxy={"rule": scenario["rule"]})

    if profile["target"] == "ticket":
        return proxy_case()
    runtime = GiteaRuntime(DEFAULT_INSTANCE_ROOT)
    with runtime, target_environment(runtime):
        item = proxy_case()
    # Copy only non-secret identity/cleanup receipts, never server data or tokens.
    save_json(Path(item["run_dir"]) / "d25-environment.json", dict(instance=runtime.identity,
        instance_directory=str(runtime.directory), cleanup=load_json(runtime.directory / "cleanup.json")))
    return item


def make_negatives(output, source, miss):
    entries = []
    mutations = ("missing_proxy_log", "wrong_run_id", "unreadable_database", "wrong_request_id")
    for label in mutations:
        directory = Path(output) / label / Path(source).name
        shutil.copytree(source, directory)
        if label == "missing_proxy_log":
            (directory / "proxy-events.jsonl").unlink()
        elif label == "unreadable_database":
            (directory / "business.sqlite").write_bytes(b"D25 deliberately unreadable SQLite evidence\n")
        else:
            name = "proxy-events.jsonl"
            data = rows(directory / name)
            if label == "wrong_run_id":
                data[0]["run_id"] = "foreign-run"
            else:
                next(r for r in data if r["event"] == "proxy_received")["request_id"] = "foreign-request"
            (directory / name).write_text("".join(__import__("json").dumps(r) + "\n" for r in data), encoding="utf-8")
        entries.append(dict(id="negative:" + label, kind="proxy", run_dir=str(directory), reject_pass=True,
            business_only=label == "unreadable_database", evidence_sha256=hashes(directory)))
    entries.append(dict(id="negative:proxy_not_hit", kind="proxy", run_dir=str(miss), reject_pass=True,
                        evidence_sha256=hashes(Path(miss))))
    return entries


def independent_check(directory, entries):
    directory = Path(directory)
    request = directory / "recheck-request.json"
    result = directory / "independent-recheck.json"
    save_json(request, dict(controller_pid=os.getpid(), entries=entries))
    step = launch(["-m", "agentcheck_biz.network_acceptance", "--worker", str(request), "--result", str(result)],
                  directory / "independent-recheck.log", timeout=600)
    if not result.is_file():
        return dict(status="ERROR", step=step, error="Independent worker did not produce a result")
    check = load_json(result)
    check["step"] = step
    if step["exit_code"] != 0 or check.get("worker_pid") != step["pid"] or check.get("worker_pid") == os.getpid():
        check["status"] = "ERROR"
    return check


def suite(output):
    directory = Path(output).resolve() / ("network-" + uuid4().hex)
    directory.mkdir(parents=True)
    matrix = support_matrix()
    slots = [dict(row, state="not_started") for row in plan()]
    save_json(directory / "support-matrix.json", matrix)
    save_json(directory / "plan.json", dict(schema_version=1, slots=deepcopy(slots), model_requests=0))
    frozen = {name: hashes(directory)[name] for name in ("support-matrix.json", "plan.json")}
    digest = implementation_digest()
    by_id = {p["id"]: p for p in profiles()}
    report = dict(status="RUNNING", started_at=datetime.now(timezone.utc).isoformat(), implementation_sha256=digest,
                  model_requests=0, frozen_sha256=frozen, slots=slots, summary_path=str(directory / "summary.json"))
    save_json(directory / "summary.json", report)
    for slot in slots:
        slot["state"] = "started"
        save_json(directory / "summary.json", report)
        try:
            item = execute(by_id[slot["profile"]], directory / "runs")
            run_dir = Path(item["run_dir"])
            slot.update(state="completed", run_dir=str(run_dir), observed=inspect_run(run_dir, slot["kind"]),
                        evidence_sha256=hashes(run_dir))
        except Exception as error:
            slot.update(state="exception", error=type(error).__name__ + ": " + str(error))
        report.update(aggregate(slots))
        save_json(directory / "summary.json", report)
        print(f"{slot['slot']}: {slot['state']} {slot.get('observed', {}).get('business_status', '')}", flush=True)
    entries = [dict(id=s["slot"], kind=s["kind"], run_dir=s["run_dir"], expected=s["expected"], require_fresh=True,
                    evidence_sha256=s["evidence_sha256"]) for s in slots if s["state"] == "completed"]
    try:
        source = next(s["run_dir"] for s in slots if s["profile"] == "C_fixed_loss_retry" and s["state"] == "completed")
        miss = next(s["run_dir"] for s in slots if s["profile"] == "ticket_P04_miss" and s["state"] == "completed")
        negatives = make_negatives(directory / "negatives", source, miss)
        report["negative_cases"] = [dict(id=e["id"], run_dir=e["run_dir"]) for e in negatives]
        report["independent_recheck"] = independent_check(directory, entries + negatives)
        if report["independent_recheck"]["status"] != "PASS":
            report["status"] = "FAIL"
    except Exception as error:
        report.update(status="ERROR", verification_error=type(error).__name__ + ": " + str(error))
    report["frozen_unchanged"] = all(hashes(directory)[name] == value for name, value in frozen.items())
    report["sources_unchanged"] = implementation_digest() == digest
    if not report["frozen_unchanged"] or not report["sources_unchanged"]:
        report["status"] = "FAIL"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save_json(directory / "summary.json", report)
    return report

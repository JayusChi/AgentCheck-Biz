"""Read saved raw evidence, not producer verdicts; usable in an offline process."""

import hashlib
import json
import os
from pathlib import Path
import sys

from agentcheck_biz.checks import load_json
from agentcheck_biz.fault_proxy.verify import assess_proxy_evidence
from agentcheck_biz.recovery.ticket_http import recheck as recheck_recovery
from agentcheck_biz.service_crash.verify import recheck as recheck_crash
from agentcheck_biz.verifiers.ticket_local import recheck_ticket_run
from agentcheck_biz.verifiers.gitea import recheck_gitea_run


def hashes(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(directory).rglob("*")) if p.is_file()}


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def inspect_run(directory, kind):
    directory = Path(directory).resolve()
    if kind not in {"business", "proxy", "recovery", "crash"}:
        raise ValueError("Unknown evidence kind")
    try:
        if kind == "crash":
            check = recheck_crash(directory)
            client = load_json(directory / "client.json")
            calls = [r["row"] for r in rows(directory / "recovery-events.jsonl") if r["row"]["event"] == "call"]
            return dict(business_status=check["business_status"], evidence_status=check["status"],
                coverage=check["coverage"], resource_count=check["resource_count"], tool_calls=len(calls), details=check)
        if kind == "recovery":
            check = recheck_recovery(directory)
            calls = [r["row"] for r in rows(directory / "recovery-events.jsonl") if r["row"]["event"] == "call"]
            return dict(business_status=check["business_status"], evidence_status=check["policy_status"],
                coverage="covered" if check["policy_status"] == "PASS" else "not_covered",
                resource_count=check["resource_count"], tool_calls=len(calls), details=check)
        run = load_json(directory / "run.json")
        gitea = run.get("object_type") == "gitea-issue"
        check = (recheck_gitea_run if gitea else recheck_ticket_run)(directory)
        if kind == "business":
            return dict(business_status=check["status"], details=check)
        coverage = assess_proxy_evidence(directory)
        final = load_json(directory / "final.json")
        if gitea:
            count = len(final["issues"]) - len(load_json(directory / "initial.json")["issues"])
        else:
            scope = load_json(directory / "case.json")["context"]
            count = sum(all(row[k] == v for k, v in scope.items()) for row in final["tickets"])
        return dict(business_status=check["status"], evidence_status=coverage["status"], coverage=coverage["coverage"],
                    resource_count=count, tool_calls=run["tool_calls"], details=dict(business=check, proxy=coverage))
    except Exception as error:
        return dict(business_status="INCONCLUSIVE", evidence_status="ERROR", coverage="not_covered",
                    resource_count=None, tool_calls=None, error=type(error).__name__ + ": " + str(error))


def accepted_entry(entry, observed, before, after):
    unchanged = before == after == entry["evidence_sha256"]
    if entry.get("reject_pass"):
        expected = observed.get("business_status") != "PASS" and observed.get("evidence_status") != "PASS"
        # A broken database may leave valid transport proof; business PASS still must be refused.
        if entry.get("business_only"):
            expected = observed.get("business_status") not in {"PASS", "FAIL"}
    else:
        expected = all(observed.get(k) == value for k, value in entry["expected"].items())
    return unchanged and expected


class ReadOnlyGuard:
    """Child-only audit guard: prohibit networking, new processes and evidence writes."""

    def __init__(self, directories):
        self.roots = tuple(Path(p).resolve() for p in directories)

    def protected(self, value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        path = Path(os.fsdecode(value)).resolve()
        return any(path == root or root in path.parents for root in self.roots)

    def __call__(self, event, args):
        if event in {"socket.connect", "socket.bind", "socket.sendto", "subprocess.Popen", "os.system"}:
            raise PermissionError("Offline evidence worker forbids " + event)
        if event == "open" and self.protected(args[0]):
            mode, flags = args[1:3]
            if (mode and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise PermissionError("Evidence is read-only")
        if event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate", "os.utime"} and self.protected(args[0]):
            raise PermissionError("Evidence is read-only")
        if event in {"os.rename", "os.link", "os.symlink"} and any(self.protected(p) for p in args[:2]):
            raise PermissionError("Evidence is read-only")
        if event == "sqlite3.connect" and "mode=ro" not in str(args[0]):
            raise PermissionError("Offline SQLite connections must explicitly use mode=ro")


def fresh_identity(directory, kind):
    name = {"proxy": "run.json", "recovery": "recovery-run.json", "crash": "crash-run.json"}[kind]
    run = load_json(directory / name)
    run_id = run["run_id"]
    if run_id != directory.name or run.get("model_called", False) or run.get("model_requests", 0) != 0:
        raise ValueError("Invalid run identity or nonzero model calls")
    initial = (load_json(directory / "observation-initial.json")["data"] if kind == "crash"
               else load_json(directory / "initial.json"))
    if initial["run_id"] != run_id:
        raise ValueError("Initial state belongs to a different run")
    identity = dict(run_id=run_id)
    if "issues" in initial:
        environment = load_json(directory / "d25-environment.json")
        instance, cleanup = environment["instance"], environment["cleanup"]
        repository = initial["repository"]
        if (initial["issues"] or not cleanup["exited"] or cleanup["pid"] != instance["pid"]
                or cleanup["instance_id"] != instance["instance_id"]
                or instance["instance_id"] not in repository["description"]
                or repository["full_name"] != instance["owner"] + "/" + run_id
                or instance["origin"] != run["proxy_service"]["upstream_origin"]):
            raise ValueError("Fresh Gitea instance/repository or owned cleanup is not proven")
        identity.update(instance_id=instance["instance_id"], repository=repository["full_name"],
                        instance_directory=environment["instance_directory"])
    else:
        case = run.get("case") or load_json(directory / "case.json")
        if any(all(row[k] == v for k, v in case["context"].items()) for row in initial["tickets"]):
            raise ValueError("Target operation was not empty at the start")
        identity["database_directory"] = str(directory)
    return identity


def worker(request):
    entries = request["entries"]
    if request["controller_pid"] == os.getpid():
        raise ValueError("Independent verification requires another process")
    sys.addaudithook(ReadOnlyGuard(e["run_dir"] for e in entries))
    results = []
    seen = {key: set() for key in ("run_id", "instance_id", "repository", "instance_directory", "database_directory")}
    for entry in entries:
        directory = Path(entry["run_dir"])
        before = hashes(directory)
        observed = inspect_run(directory, entry["kind"])
        after = hashes(directory)
        accepted = accepted_entry(entry, observed, before, after)
        freshness = None
        if entry.get("require_fresh"):
            try:
                freshness = fresh_identity(directory, entry["kind"])
                for key, value in freshness.items():
                    if value in seen[key]:
                        raise ValueError("Repeated initial environment: " + key)
                    seen[key].add(value)
            except Exception as error:
                freshness = dict(error=type(error).__name__ + ": " + str(error))
                accepted = False
        results.append(dict(id=entry["id"], run_dir=str(directory), kind=entry["kind"], observed=observed,
            accepted=accepted, freshness=freshness, evidence_unchanged=before == after == entry["evidence_sha256"],
            evidence_sha256=after))
    return dict(status="PASS" if results and all(r["accepted"] for r in results) else "FAIL",
        controller_pid=request["controller_pid"], worker_pid=os.getpid(), offline=True, read_only=True, model_requests=0,
        planned=len(entries), executed=len(results), results=results)

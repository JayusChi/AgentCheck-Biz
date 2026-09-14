"""Reconstruct causal proof from process evidence, never from a scenario label."""

from agentcheck_biz.checks import load_json
from agentcheck_biz.verifiers.mcp import read_log
from agentcheck_biz.observers.gitea import normalize_issue
from .barrier import KEYS, bound


def require(value, reason):
    if not value:
        raise ValueError("Commit-loss evidence: " + reason)


def verify_confirmation(directory, run, proxy, target):
    require(run.get("commit_loss_enabled") is True and run["commit_loss_version"] == 1,
            "confirmation protocol was not enabled")
    ready = load_json(directory / "commit-loss-proxy-ready.json")
    decision = load_json(directory / "commit-loss-decision.json")
    ack = load_json(directory / "commit-loss-aborted.json")
    identity = {k: ready[k] for k in KEYS}
    require(identity["run_id"] == run["run_id"] and identity["operation_id"] == run["context"]["operation_id"]
            and identity["call_id"] == "call-1" and identity["kind"] == run["commit_loss_kind"]
            and {k: identity[k] for k in ("kind", "nonce")} == target["confirmation"], "barrier root binding mismatch")
    bound(decision, identity)
    bound(ack, identity)
    require(decision["confirmed"] is True and ack["aborted"] is True
            and decision["pid"] == run["runner_pid"]
            and ready["pid"] == ack["pid"] == run["proxy_service"]["pid"], "independent decision/abort ownership missing")
    log = read_log(directory, "commit-loss-events.jsonl", run)
    require([r["event"] for r in log] == ["confirmation_requested",
            "commit_confirmed" if identity["kind"] == "sqlite_commit" else "api_visibility_confirmed",
            "abort_acknowledged", "controller_stopped"]
            and all(r["pid"] == run["runner_pid"] for r in log), "controller did not complete confirmation and acknowledgement")
    proof = log[1]
    bound(proof, identity)
    require(proof["record"] == decision["record"] and proof["source"] == decision["source"] == target["observer_source"],
            "independent proof differs from granted decision")
    selected = [r for r in proxy if r.get("call_id") == "call-1"]
    trace = {r["event"]: r for r in selected}
    received, confirmed, abort = (trace[k] for k in ("proxy_received", "confirmation_received", "downstream_aborted"))
    bound({**received, **target["confirmation"]}, identity)
    # Windows Python 3.10 monotonic can share a ~15 ms clock tick. Sequence
    # references and atomic handshakes prove strict order; clocks must not reverse.
    require(ready["monotonic_ns"] <= log[0]["monotonic_ns"] <= proof["monotonic_ns"]
            <= decision["monotonic_ns"] <= confirmed["monotonic_ns"] <= abort["monotonic_ns"]
            <= ack["monotonic_ns"] <= log[2]["monotonic_ns"]
            and confirmed["seq"] < trace["fault_triggered"]["seq"] < abort["seq"]
            and confirmed["decision_monotonic_ns"] == decision["monotonic_ns"], "confirmation/abort causal order missing")
    case = load_json(directory / "case.json")
    parent = read_log(directory, "events.jsonl", run)
    calls = [r for r in parent if r["event"] == "tool_called"]
    unknown = [r for r in parent if r["event"] == "client_outcome_unknown"]
    require(len(unknown) == 1 and unknown[0]["call_id"] == "call-1"
            and unknown[0]["attempt_id"] == identity["attempt_id"] and unknown[0]["operation_id"] == identity["operation_id"]
            and unknown[0]["code"] == "OUTCOME_UNKNOWN", "client unknown outcome missing")
    clients = [r for name in target["client_logs"] for r in read_log(directory, name, run)]
    errors = [r for r in clients if r["event"] in {"http_request_failed", "api_error"}]
    require(len(errors) == 1 and errors[0]["attempt_id"] == identity["attempt_id"]
            and errors[0]["error_type"] == unknown[0]["error_type"] in {"RemoteProtocolError", "ReadError"}
            and calls[0]["time_utc"] < proof["time_utc"] < abort["time_utc"] <= errors[0]["time_utc"] <= unknown[0]["time_utc"],
            "actual client disconnect does not follow independent confirmation")
    require(not any(r["event"] == "tool_result_delivered" and r["call_id"] == "call-1" for r in parent),
            "lost response was delivered to the client")
    final = load_json(directory / "final.json")
    if identity["kind"] == "sqlite_commit":
        receipt = load_json(directory / "commit-loss-ticket-ready.json")
        release = load_json(directory / "commit-loss-release.json")
        bound(receipt, identity)
        bound(release, identity)
        service = read_log(directory, "http-service-events.jsonl", run)
        waiting = [r for r in service if r["event"] == "commit_barrier_waiting"]
        released = [r for r in service if r["event"] == "commit_barrier_released"]
        effects = [r for r in service if r["event"] in {"ticket_created", "ticket_replayed"}]
        require(len(waiting) == len(released) == 1 and all(waiting[0][k] == v for k, v in receipt.items())
                and receipt["pid"] == run["http_service"]["pid"] != decision["pid"]
                and receipt["deduplicated"] is False and receipt["ticket"] == decision["record"] == effects[0]["ticket"]
                and effects[0]["seq"] < waiting[0]["seq"] < released[0]["seq"]
                and receipt["monotonic_ns"] == decision["service_commit_monotonic_ns"] <= proof["monotonic_ns"]
                and ack["monotonic_ns"] <= released[0]["monotonic_ns"]
                and release["confirmed"] is True and released[0]["confirmed"] is True,
                "service did not wait between committed receipt and acknowledged abort")
        require(decision["snapshot"] == proof["snapshot"] and decision["snapshot"]["run_id"] == run["run_id"]
                and decision["record"] in decision["snapshot"]["tickets"]
                and decision["record"] in final["tickets"]
                and all(decision["record"][k] == v for k, v in {**case["context"], **case["request"]}.items()),
                "read-only checkpoint does not support the bound committed ticket")
        retries = [r for r in parent if r["event"] == "commit_loss_retry_scheduled"]
        require(len(calls) == len(effects) == 2 and len(retries) == 1
                and unknown[0]["seq"] < retries[0]["seq"] < calls[1]["seq"]
                and retries[0]["prior_call_id"] == "call-1" and retries[0]["maximum_attempts"] == 2
                and all(c["operation_id"] == identity["operation_id"] and c["deadline"] == run["context"]["deadline"] for c in calls)
                and calls[0]["attempt_id"] != calls[1]["attempt_id"], "same-operation bounded retry is missing")
        require(effects[1]["event"] == ("ticket_replayed" if run["app_version"] == "fixed" else "ticket_created")
                and (effects[1]["ticket"]["ticket_id"] == receipt["ticket"]["ticket_id"]) == (run["app_version"] == "fixed"),
                "retry receipt contradicts the repaired/unsafe service")
    else:
        import hashlib
        import json
        import re
        from agentcheck_biz.gitea_cases import issue_body
        from agentcheck_biz.adapters.contracts import RunContext
        context = RunContext(**{**run["context"], "evidence_dir": directory})
        response = load_json(directory / "commit-loss-upstream.json")
        bound(response, identity)
        require(re.fullmatch(r"gitea-api/observer-[1-9][0-9]*\.json", decision["api_evidence"]), "unsafe API evidence reference")
        raw = load_json(directory / decision["api_evidence"])
        repository = target["identity"]["repository"]
        require(json.loads(response["raw_body"]) == response["body"]
                and hashlib.sha256(response["raw_body"].encode("utf-8")).hexdigest() == trace["upstream_response"]["body_sha256"],
                "saved upstream body differs from bytes received by proxy")
        require(normalize_issue(raw["body"], repository) == normalize_issue(response["body"], repository) == decision["record"]
                and decision["record"] in final["issues"] and response["status_code"] == 201
                and decision["record"]["title"] == case["request"]["title"]
                and decision["record"]["body"] == issue_body(context, context.operation_id, case["request"]["body"])
                and trace["upstream_response"]["monotonic_ns"] < proof["monotonic_ns"], "API-visible resource is unsupported")
        observer = read_log(directory, "gitea-observer.jsonl", run)
        api = [r for r in observer if r.get("evidence") == decision["api_evidence"] and r["event"] == "api_request"]
        require(len(api) == 1 and api[0]["method"] == "GET" and api[0]["origin"] == target["origin"]
                and api[0]["pid"] == decision["pid"] and api[0]["time_utc"] < proof["time_utc"]
                and api[0]["path"] == raw["path"] == f"/api/v1/repos/{repository['full_name']}/issues/{decision['record']['number']}"
                and raw["method"] == "GET" and raw["role"] == "observer" and raw["status_code"] == 200
                and raw["request_id"] == api[0]["request_id"], "API confirmation did not bypass proxy")
        terminal = [r for r in observer if r["event"] == "api_response" and r["request_id"] == raw["request_id"]]
        require(len(terminal) == 1 and terminal[0]["status_code"] == 200
                and terminal[0]["evidence"] == decision["api_evidence"]
                and terminal[0]["time_utc"] <= proof["time_utc"], "independent GET had no successful response before confirmation")
        require(len(calls) == 1 and not any(r["event"] == "commit_confirmed" for r in log), "Gitea incorrectly claims internal commit/retry")
    detail = {k: unknown[0][k] for k in ("code", "error_type", "call_id", "attempt_id", "operation_id")}
    from agentcheck_biz.verifiers.mcp import verify_mcp_evidence
    verify_mcp_evidence(directory, run, {"call-1": detail})
    return {"check_id": "confirmed_response_loss", "expected": "独立确认先于真实断连，客户端结果未知，证据逐跳关联",
            "actual": {"coverage": "covered", "evidence_level": identity["kind"], "record": decision["record"],
                       "client_network_error": detail}, "passed": True,
            "evidence": ["commit-loss-events.jsonl", "commit-loss-decision.json", "commit-loss-aborted.json",
                         "proxy-events.jsonl", "mcp-1-wire.jsonl", "observation-final.json"]}


def unknown_calls(directory, run):
    if not run.get("commit_loss_enabled"):
        return {}
    rows = read_log(directory, "events.jsonl", run)
    return {r["call_id"]: {k: r[k] for k in ("code", "error_type", "call_id", "attempt_id", "operation_id")}
            for r in rows if r["event"] == "client_outcome_unknown"}

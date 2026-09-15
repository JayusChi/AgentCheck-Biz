"""Validate saved cross-process transport evidence before the unchanged oracle."""

import json

from agentcheck_biz.adapters.contracts import ObservationError
from agentcheck_biz.checks import load_json, observe_database


def verify_http_evidence(directory, run, unknown_calls=None):
    unknown_calls = unknown_calls or {}
    def require(value, reason):
        if not value:
            raise ObservationError("HTTP evidence: " + reason)

    def log(name):
        rows = [json.loads(line) for line in (directory / name).read_text(encoding="utf-8").splitlines() if line.strip()]
        require(rows and all(row["seq"] == n and row["run_id"] == run["run_id"]
                            for n, row in enumerate(rows, 1)), "missing or mixed event stream: " + name)
        return rows

    identity = load_json(directory / "http-ready.json")
    require(identity == run["http_service"] and identity["run_id"] == run["run_id"]
            and identity["service"] == "ticket-http" and identity["api_version"] == 1
            and identity["app_version"] == run["app_version"]
            and identity["implementation_sha256"] == run["implementation_sha256"]
            and identity["host"] == "127.0.0.1" and 0 < identity["port"] <= 65535
            and type(identity["pid"]) is int and identity["pid"] != run["runner_pid"], "service version or process identity mismatch")
    probes = load_json(directory / "http-probes.json")
    require(probes == {"/health": {"status_code": 200, "body": {"status": "ok", **identity}},
                       "/version": {"status_code": 200, "body": identity}}, "health/version not verified over HTTP")
    cleanup = load_json(directory / "http-cleanup.json")
    require(all(cleanup[key] == identity[key] for key in ("run_id", "instance_id", "pid"))
            and cleanup["owner_pid"] == run["runner_pid"] and cleanup["exited"] is True
            and type(cleanup["returncode"]) is int, "owned process cleanup missing")
    observer = log("http-observer.jsonl")
    require(len(observer) == 2 and all(row["event"] == "state_observed" and row["pid"] == run["runner_pid"]
            and row["pid"] != identity["pid"] and row["observation"] == load_json(directory / f"observation-{phase}.json")
            for row, phase in zip(observer, ("initial", "final"))), "observer did not independently capture both states")
    final = load_json(directory / "final.json")
    require(observe_database(directory / "business.sqlite") == final, "current database differs from final observation")
    events, service = log("events.jsonl"), log("http-service-events.jsonl")
    require(service[0]["event"] == "service_initialized" and service[0]["identity"] == identity,
            "service startup identity missing")
    started = [row for row in events if row["event"] == "http_process_created"]
    stopped = [row for row in events if row["event"] == "http_process_stopped"]
    require(len(started) == len(stopped) == 1 and started[0]["owner_pid"] == run["runner_pid"]
            and all(row["pid"] == identity["pid"] and row["instance_id"] == identity["instance_id"]
                    for row in started + stopped), "process ownership trace missing")
    transport_events = list(events)
    is_mcp = run["plugin"]["name"] == "ticket-mcp"
    if is_mcp:
        for name in run["mcp_connections"]:
            transport_events.extend(log(name + "-http.jsonl"))
        transport_events.sort(key=lambda row: row["time_utc"])
    requests = [row for row in transport_events if row["event"] == "http_request_started"]
    responses = [row for row in transport_events if row["event"] == "http_response_received"]
    failures = [row for row in transport_events if row["event"] == "http_request_failed"]
    require({r["request_id"] for r in failures} == set(unknown_calls)
            and len(failures) == len(unknown_calls)
            and all(r["error_type"] == unknown_calls[r["request_id"]]["error_type"] for r in failures),
            "unexpected or unsupported HTTP failures")
    received = [row for row in service if row["event"] == "request_received"]
    completed = [row for row in service if row["event"] == "request_completed"]
    require(len(requests) == len(responses) + len(failures) == len(received) == len(completed) == run["http_request_attempts"]
            and len({row["request_id"] for row in requests}) == len(requests)
            and run["transport"] == ("mcp-stdio" if is_mcp else "http") and run["http_auto_retries"] == 0,
            "request counts or retry policy mismatch")
    key = lambda row: (row["request_id"], row["attempt_id"], row["method"], row["path"])
    require([key(row) for row in requests] == [key(row) for row in received]
            and [key(row) + (row["status_code"],) for row in responses]
            == [key(row) + (row["status_code"],) for row in completed if row["request_id"] not in unknown_calls]
            and [key(row) for row in requests if row["request_id"] not in unknown_calls] == [key(row) for row in responses]
            and all(row["pid"] == identity["pid"] for row in received + completed), "request/response correlation mismatch")
    require([(row["request_id"], row["path"]) for row in requests[:2]]
            == [("probe-health", "/health"), ("probe-version", "/version")], "probe requests missing")
    calls = [row for row in events if row["event"] == "tool_called"]
    business = [row for row in requests if row["path"] == "/tickets"]
    attempts = {row["call_id"]: row["attempt_id"] for row in calls}
    require(all(row["attempt_id"] == (attempts[row["request_id"]] if is_mcp and row["path"] == "/tickets"
            else run["context"]["attempt_id"]) for row in requests), "HTTP attempt context mismatch")
    require(len(business) == len(calls) == run["tool_calls"] == run["http_business_request_attempts"]
            and len(requests) == len(business) + 2
            and [(row["request_id"], row["method"]) for row in business]
            == [(row["call_id"], {"create_ticket": "POST", "query_tickets": "GET"}[row["tool"]]) for row in calls],
            "tool calls do not match actual business requests")
    effects = [row for row in service if row["event"] in {"ticket_created", "ticket_replayed"}]
    successes = [row for row in completed if row["method"] == "POST" and row["status_code"] == 200]
    effect_ids, success_ids = ([row["request_id"] for row in group] for group in (effects, successes))
    # A lost response may finish its server middleware after the retry response.
    # Correlate receipts by unique request ID; retain the old strict order otherwise.
    require((sorted(effect_ids) == sorted(success_ids) and len(set(effect_ids)) == len(effect_ids)) if unknown_calls
            else effect_ids == success_ids,
            "successful writes lack service receipts")
    delivered = {row["call_id"]: row for row in events if row["event"] == "tool_result_delivered"}
    for effect in effects:
        call = next(row for row in calls if row["call_id"] == effect["request_id"])
        ticket = effect["ticket"]
        require(effect["pid"] == identity["pid"] and ticket in final["tickets"]
                and (effect["request_id"] in unknown_calls and effect["request_id"] not in delivered
                     or delivered.get(effect["request_id"], {}).get("result") == ticket)
                and all(ticket[field] == call[field] for field in ("tenant_id", "operation_id")),
                "returned receipt is not supported by independently observed database state")
    return {"check_id": "http_cross_process_evidence", "expected": "独立进程 HTTP 请求、版本、只读观察与清理相互对应",
            "actual": {"runner_pid": run["runner_pid"], "service_pid": identity["pid"],
                       "http_requests": len(requests), "business_requests": len(business)}, "passed": True,
            "evidence": ["http-ready.json", "http-probes.json", "http-service-events.jsonl",
                         "http-observer.jsonl", "http-cleanup.json", "events.jsonl", "business.sqlite"]}

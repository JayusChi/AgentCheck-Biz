"""Read-only transport coverage; never confuse fault coverage with business PASS."""

import json
from pathlib import Path
import re

from agentcheck_biz.adapters.contracts import Observation, RunContext
from agentcheck_biz.checks import load_json, verdict
from .rules import loopback, matches, validate_rule


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def assess_proxy_evidence(directory, run=None):
    directory = Path(directory).resolve()
    try:
        run = run or load_json(directory / "run.json")
        require(run.get("proxy_enabled") is True and run["proxy_version"] == 1, "Proxy was not enabled")
        context = RunContext(**{**run["context"], "evidence_dir": directory})

        def log(name):
            require(isinstance(name, str) and re.fullmatch(r"[a-z0-9_-]+\.jsonl", name), "Unsafe log reference")
            rows = [json.loads(line) for line in (directory / name).read_text(encoding="utf-8").splitlines() if line.strip()]
            require(all(row["seq"] == n and row["run_id"] == context.run_id for n, row in enumerate(rows, 1)), "Mixed event stream")
            return rows

        identity, target, cleanup, summary = (load_json(directory / ("proxy-" + name + ".json")) for name in ("ready", "target", "cleanup", "summary"))
        rule = validate_rule(load_json(directory / "proxy-rule.json"))
        address = loopback(target["origin"])
        require(identity == run["proxy_service"] and identity["run_id"] == context.run_id and identity["version"] == 1
                and identity["implementation_sha256"] == run["implementation_sha256"] and identity["host"] == "127.0.0.1"
                and 0 < identity["port"] <= 65535 and identity["port"] != address.port
                and identity["parent_pid"] == run["runner_pid"] and identity["pid"] != run["runner_pid"]
                and identity["upstream_origin"] == target["origin"], "Proxy identity or pinned target mismatch")
        proxy_origin = f"http://127.0.0.1:{identity['port']}"
        require(cleanup["owner_pid"] == run["runner_pid"] and cleanup["exited"] is True and cleanup["returncode"] == 0
                and all(cleanup[key] == identity[key] for key in ("run_id", "pid", "nonce")), "Owned proxy cleanup missing")
        events = log("proxy-events.jsonl")
        require(events and events[0]["event"] == "proxy_started" and events[0]["identity"] == identity
                and events[-1]["event"] == "proxy_stopped" and events[-1]["summary"] == summary
                and all(row["pid"] == identity["pid"] for row in events)
                and [r["monotonic_ns"] for r in events] == sorted(r["monotonic_ns"] for r in events), "Proxy lifecycle log incomplete")
        parent = log("events.jsonl")
        for event in ("proxy_process_created", "proxy_process_stopped"):
            rows = [row for row in parent if row["event"] == event]
            require(len(rows) == 1 and all(rows[0][key] == identity[key] for key in ("pid", "nonce")), "Proxy ownership trace missing")
        calls = [row for row in parent if row["event"] == "tool_called"]
        clients = [row for name in target["client_logs"] for row in log(name)]
        requests = [r for r in clients if r["event"] in {"http_request_started", "api_request"}]
        client_pids = set()
        for name in run["mcp_connections"]:
            require(re.fullmatch(r"mcp-[1-9][0-9]*", name), "Invalid MCP connection reference")
            session = load_json(directory / (name + "-session.json"))
            stopped = load_json(directory / (name + "-cleanup.json"))
            require(session["identity"]["run_id"] == context.run_id and session["identity"]["parent_pid"] == run["runner_pid"]
                    and session["initialize"]["protocolVersion"] == run["mcp_protocol_version"]
                    and stopped["pid"] == session["identity"]["pid"] and stopped["exited"] is True and stopped["returncode"] == 0,
                    "MCP client process handshake or cleanup missing")
            client_pids.add(session["identity"]["pid"])
        errors = {r["request_id"]: r for r in clients if r["event"] in {"http_request_failed", "api_error"}}
        responses = {r["request_id"]: r for r in clients if r["event"] in {"http_response_received", "api_response"}}
        received = [r for r in events if r["event"] == "proxy_received"]
        connections = [r for r in events if r["event"] == "connection_accepted"]
        closed = [r for r in events if r["event"] == "connection_closed"]
        upstream = [r for r in events if r["event"] == "upstream_connecting"]
        triggered = [r for r in events if r["event"] == "fault_triggered"]
        require(len(connections) == len(closed) == len(received) == len(requests) == len(calls) == run["tool_calls"] == summary["connections"]
                and len(upstream) == summary["upstream_attempts"] and len(triggered) == summary["triggers"]
                and len({r["connection_id"] for r in received}) == len(received)
                and len({r["attempt_id"] for r in received}) == len(received)
                and len({r["upstream_attempt_id"] for r in upstream}) == len(upstream)
                and all(r["origin"] == target["origin"] for r in upstream), "Connection counts, attempt IDs or upstream changed")
        require(set(errors).isdisjoint(responses) and len(errors) + len(responses) == len(requests), "Client attempts lack terminal outcomes")
        require(not any(r["event"] in {"scope_rejected", "proxy_error"} for r in events), "Unexpected proxy error or scope rejection")
        for phase in ("initial", "final"):
            observation = Observation(**load_json(directory / ("observation-" + phase + ".json")))
            observation.require_complete(context)
            require(observation.source == target["observer_source"] and observation.data == load_json(directory / (phase + ".json")), "Independent observation missing")
        for name in target["observer_http_logs"]:
            observer = [r for r in log(name) if r["event"] == "api_request"]
            require(observer and all(r["method"] == "GET" and r["origin"] == target["origin"] and r["pid"] == run["runner_pid"] for r in observer),
                    "Observer did not bypass the proxy")
        count, seen_hits = {}, 0
        fields = ("body_sha256", "body_bytes", "headers_sha256")
        evidence_errors = []
        for item in received:
            call = next(r for r in calls if r["call_id"] == item["call_id"])
            client = next(r for r in requests if r["request_id"] == item["request_id"])
            require(all(call[k] == item[k] for k in ("call_id", "attempt_id", "operation_id", "tool"))
                    and client["attempt_id"] == item["attempt_id"] and client["origin"] == proxy_origin
                    and client["pid"] in client_pids and client["pid"] != identity["pid"] and client["pid"] != run["runner_pid"]
                    and client["method"] == item["method"] and client["path"] == item["path"], "Client/MCP/proxy request correlation mismatch")
            count[item["tool"]] = count.get(item["tool"], 0) + 1
            require(item["request_number"] == count[item["tool"]], "Per-tool request number mismatch")
            trace = [r for r in events if r.get("connection_id") == item["connection_id"]]
            by_event = {r["event"]: r for r in trace}
            hit = by_event.get("fault_triggered")
            selected = matches(rule, context.run_id, item["tool"], item["request_number"], seen_hits)
            denied = bool(rule and rule["schema_version"] == 2 and "confirmation_denied" in by_event)
            require(bool(hit) == (selected and not denied), "Fault rule did not match recorded request/limit")
            if hit:
                seen_hits += 1
                require(all(hit[k] == rule[k] for k in ("rule_id", "phase", "action", "delay_ms"))
                        and hit["trigger_number"] == seen_hits, "Fault stage or cap mismatch")
            forwarded = by_event.get("upstream_forwarded")
            upstream_response = by_event.get("upstream_response")
            sent = by_event.get("downstream_sent")
            rejected = hit and hit["action"] == "reject"
            commit_drop = bool(hit and rule["schema_version"] == 2 and target["confirmation"]["kind"] == "sqlite_commit")
            if rejected:
                require(not forwarded and not upstream_response and "upstream_connecting" not in by_event
                        and item["seq"] < hit["seq"] < by_event["downstream_aborted"]["seq"], "Reject occurred after upstream forwarding")
            elif commit_drop:
                require(forwarded and not upstream_response and all(forwarded[k] == item[k] for k in fields)
                        and forwarded["seq"] < by_event["confirmation_received"]["seq"] < hit["seq"],
                        "Ticket drop lacks a blocked upstream and independent confirmation")
            else:
                require(forwarded and upstream_response and all(forwarded[k] == item[k] for k in fields)
                        and item["seq"] < forwarded["seq"] < upstream_response["seq"], "Transparent request or upstream response missing")
            if hit and hit["action"] in {"reject", "drop"}:
                aborted = by_event["downstream_aborted"]
                require(not sent and aborted["bytes_sent"] == 0 and aborted["phase"] == hit["phase"]
                        and item["request_id"] in errors and errors[item["request_id"]]["error_type"] in {"RemoteProtocolError", "ReadError"},
                        "Abort lacks an actual HTTP client network error")
                if not rejected and not commit_drop:
                    require(upstream_response["seq"] < hit["seq"] < aborted["seq"], "Drop occurred before full upstream response")
            elif hit:
                require(upstream_response["seq"] < hit["seq"] < by_event["delay_started"]["seq"], "Delay started before upstream response")
                if item["request_id"] in errors:
                    require(errors[item["request_id"]]["error_type"] in {"ReadTimeout", "TimeoutError"} and not sent, "Delay lacks real client timeout")
                else:
                    finished = by_event["delay_finished"]
                    require(sent and (finished["monotonic_ns"] - by_event["delay_started"]["monotonic_ns"]) / 1e6 >= rule["delay_ms"], "Delay duration not reached")
            else:
                require(sent and item["request_id"] in responses, "Transparent downstream did not complete")
            if sent:
                require(upstream_response["status_code"] == sent["status_code"] == responses[item["request_id"]]["status_code"]
                        and all(sent[k] == upstream_response[k] for k in fields), "Response changed in transparent forwarding")
            if item["request_id"] in errors:
                evidence_errors.append({"call_id": item["call_id"], "attempt_id": item["attempt_id"], "error_type": errors[item["request_id"]]["error_type"]})
        require(count == summary["tool_requests"], "Tool ordinal summary mismatch")
        coverage = "transparent" if rule is None else "covered" if triggered else "not_covered"
        require(coverage == summary["coverage"], "Coverage summary mismatch")
        check = {"check_id": "network_proxy_contract", "expected": "透明转发或指定阶段的真实故障与独立观察",
                 "actual": {"coverage": coverage, "triggers": len(triggered), "connections": len(connections),
                            "upstream_attempts": len(upstream), "client_network_errors": evidence_errors},
                 "passed": coverage != "not_covered", "evidence": ["proxy-rule.json", "proxy-events.jsonl", "proxy-summary.json",
                     "proxy-ready.json", "proxy-cleanup.json", *target["client_logs"], "observation-final.json"]}
        checks = [check]
        if rule and rule["schema_version"] == 2 and coverage == "covered":
            from agentcheck_biz.commit_loss.verify import verify_confirmation
            try:
                checks.append(verify_confirmation(directory, run, events, target))
            except FileNotFoundError:
                check["passed"] = False
                check["actual"]["coverage"] = "not_covered"
                return {**verdict("INCONCLUSIVE", "独立确认链证据缺失；不能计入覆盖", [check]), "coverage": "not_covered"}
        result = verdict("INCONCLUSIVE" if coverage == "not_covered" else "PASS",
                         "未覆盖：故障规则未命中或独立确认失败，不能作为故障验收通过" if coverage == "not_covered" else "代理传输契约通过；业务结果另见 checks.json", checks)
        return {**result, "coverage": coverage}
    except Exception as error:
        return {**verdict("ERROR", f"Proxy evidence error: {type(error).__name__}: {error}", []), "coverage": "invalid"}

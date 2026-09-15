"""Independent saved-evidence oracle; an HTTP response cannot prove rollback."""

import json
from pathlib import Path
import sqlite3

from agentcheck_biz.checks import load_json
from agentcheck_biz.recovery.verify import audit_trace
from . import POINTS, VERSION
from .observer import read_state


def recheck(directory):
    directory = Path(directory)
    try:
        run = load_json(directory / "crash-run.json")
        run_id, point = run["run_id"], run["point"]
        def require(value, message):
            if not value:
                raise ValueError(message)

        require(run["version"] == VERSION and run["schema_version"] == 1 and point in POINTS, "unsupported crash schema/point")
        def rows(path, allow_empty=False):
            result = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            require((result or allow_empty) and all(r["seq"] == n and r["run_id"] == run_id for n, r in enumerate(result, 1)), "mixed/incomplete event log")
            return result

        events = rows(directory / "events.jsonl")
        cleanup = load_json(directory / "cleanup.json")
        require(cleanup["run_id"] == run_id and cleanup["allocated"] == cleanup["exited"] and not cleanup["failures"], "incomplete cleanup")
        execution = load_json(directory / "execution.json")
        if execution["status"] != "completed":
            return {"status": "INCONCLUSIVE", "coverage": "not_covered", "business_status": "INCONCLUSIVE",
                    "persistence": "unverified", "reason": execution.get("error_type", "execution_incomplete"),
                    "resource_count": None}
        allocations = [r for r in events if r["event"] == "process_allocated"]
        healthy = [r for r in events if r["event"] == "generation_healthy"]
        require(len(allocations) == len(healthy) == 2, "missing initial/restarted service")
        require([r["generation"] for r in allocations] == [1, 2] and allocations[0]["instance_id"] != allocations[1]["instance_id"], "generation did not change")
        # PID reuse by the OS is allowed only with a new instance and old exit proof.
        stored_root = Path(run["context"]["evidence_dir"])
        data_dir = str(stored_root / "data")
        service_logs, identities = [], []
        for allocation, health in zip(allocations, healthy):
            instance = directory / "instances" / allocation["instance_id"]
            ready, exit_record = (load_json(instance / name) for name in ("ready.json", "exit.json"))
            saved_allocation = load_json(instance / "allocation.json")
            require(all(allocation[k] == v for k, v in saved_allocation.items()), "allocation receipt differs from owner log")
            identities.append(ready)
            keys = ("run_id", "instance_id", "generation", "nonce", "pid", "owner_pid", "data_dir")
            require(all(ready[k] == allocation[k] == exit_record[k] for k in keys), "owned process identity mismatch")
            require(ready == health["identity"] and ready["owner_pid"] == run["runner_pid"] != ready["pid"]
                    and ready["data_dir"] == data_dir and ready["implementation_sha256"] == run["implementation_sha256"]
                    and ready["service"] == "ticket-http" and ready["api_version"] == 1 and ready["app_version"] == "fixed"
                    and ready["host"] == "127.0.0.1" and type(ready["port"]) is int and 0 < ready["port"] < 65536,
                    "service identity/version/data directory mismatch")
            require(exit_record["exited"] is True and type(exit_record["returncode"]) is int, "missing process exit proof")
            probes = load_json(instance / "probes.json")
            require(probes == {"/health": {"status_code": 200, "body": {"status": "ok", **ready}},
                               "/version": {"status_code": 200, "body": ready}}, "restart health/version not verified")
            log = rows(instance / "service-events.jsonl")
            service_logs.append(log)
            probe_ids = [f"generation-{ready['generation']}-{name}" for name in ("health", "version")]
            probe_requests = [r for r in log if r["event"] == "request_received" and r["path"] != "/tickets"]
            probe_completions = [r for r in log if r["event"] == "request_completed" and r["path"] != "/tickets"]
            parent_requests = [r for r in events if r["event"] == "http_request_started" and r["request_id"] in probe_ids]
            parent_responses = [r for r in events if r["event"] == "http_response_received" and r["request_id"] in probe_ids]
            require([r["request_id"] for r in probe_requests] == [r["request_id"] for r in probe_completions]
                    == [r["request_id"] for r in parent_requests] == [r["request_id"] for r in parent_responses] == probe_ids
                    and all(r["status_code"] == 200 for r in probe_completions + parent_responses), "health/version lacks actual HTTP exchange")
            opens = [r for r in log if r["event"] in {"database_seeded", "database_resumed"}]
            require(len(opens) == 1 and opens[0]["event"] == ("database_seeded" if ready["generation"] == 1 else "database_resumed")
                    and opens[0]["data_dir"] == data_dir, "database was reseeded on restart")
            require(any(r["event"] == "service_initialized" and r["identity"] == ready for r in log), "missing service startup receipt")
        require(healthy[0]["database_file_id"] == healthy[1]["database_file_id"], "database file replaced during restart")
        killed = [r for r in events if r["event"] == "process_exited" and r["generation"] == 1]
        verified = [r for r in events if r["event"] == "crash_identity_verified"]
        require(len(killed) == len(verified) == 1 and killed[0]["reason"] == "fault_kill" and not killed[0]["already_exited"]
                and verified[0]["identity"] == identities[0] and killed[0]["returncode"] != 0
                and healthy[0]["seq"] < verified[0]["seq"] < killed[0]["seq"] < allocations[1]["seq"] < healthy[1]["seq"], "crash/restart order not proven")
        first_dir = directory / "instances" / identities[0]["instance_id"]
        armed, barrier = load_json(first_dir / "armed.json"), load_json(first_dir / "barrier.json")
        require(all(barrier[k] == v for k, v in armed.items()) and verified[0]["barrier"] == barrier
                and armed["point"] == point and armed["operation_id"] == run["context"]["operation_id"]
                and all(armed[k] == identities[0][k] for k in ("pid", "nonce", "generation", "instance_id", "data_dir", "run_id"))
                and not (first_dir / "release.json").exists(), "foreign or released barrier")
        require(not any(r["event"] in {"crash_barrier_released", "crash_barrier_expired"} for r in service_logs[0]), "barrier no longer held at crash")
        scope, request = run["case"]["context"], run["case"]["request"]
        observations = {}
        observation_events = [r for r in events if r["event"] == "independent_observation"]
        phase_sequences = {}
        for phase in ("initial", "at_barrier", "after_exit", "after_restart", "final"):
            saved = load_json(directory / ("observation-" + phase + ".json"))
            matches = [r for r in observation_events if r["observation"]["phase"] == phase]
            require(len(matches) == 1 and matches[0]["observation"] == saved
                    and saved["run_id"] == run_id and saved["pid"] == run["runner_pid"]
                    and saved["operation_id"] == scope["operation_id"] and saved["source"] == "sqlite-mode-ro"
                    and saved["database"] == str(stored_root / "data" / "business.sqlite"), "independent observation missing/foreign")
            if phase != "after_exit":
                require(saved["complete"] is True and saved["data"]["run_id"] == run_id, "required state unreadable")
            elif not saved["complete"]:
                require(saved["data"] is None and bool(saved.get("error")), "failed observation treated as empty")
            observations[phase] = saved
            phase_sequences[phase] = matches[0]["seq"]
        require(healthy[0]["seq"] < phase_sequences["initial"] < phase_sequences["at_barrier"] < verified[0]["seq"]
                < killed[0]["seq"] < phase_sequences["after_exit"] < allocations[1]["seq"]
                < healthy[1]["seq"] < phase_sequences["after_restart"] < phase_sequences["final"], "observation/crash/restart ordering")
        def selected(state, table):
            return [r for r in state[table] if all(r[k] == v for k, v in scope.items())]
        initial, at_barrier, restarted, final = [observations[p]["data"] for p in ("initial", "at_barrier", "after_restart", "final")]
        require(initial["tickets"] == sorted(run["case"]["initial_tickets"], key=lambda r: r["ticket_id"]), "initial fixture differs from case")
        if observations["after_exit"]["complete"]:
            require(observations["after_exit"]["data"] == restarted, "post-exit data differs from resumed data")
        require(not selected(initial, "tickets") and not selected(initial, "idempotency_keys"), "case must start without the operation")
        for state in (at_barrier, restarted, final):
            for table in ("tickets", "idempotency_keys"):
                unrelated = lambda data: [r for r in data[table] if not all(r[k] == v for k, v in scope.items())]
                require(unrelated(initial) == unrelated(state), "unrelated tenant state changed")
        expected_count = 1 if point == "after_commit" else 0
        require(all(len(selected(state, table)) == expected_count for state in (at_barrier, restarted)
                    for table in ("tickets", "idempotency_keys")), "rollback/commit did not match crash point")
        first_requests = [r for r in service_logs[0] if r["event"] == "request_received" and r["path"] == "/tickets"]
        if point == "before_service":
            gate = [r for r in events if r["event"] == "request_gate_held"]
            require(not first_requests and len(gate) == 1 and gate[0]["barrier"] == barrier
                    and barrier["gate"] == "before_http_dispatch" and barrier["publisher_pid"] == run["runner_pid"], "request entered service before gate")
        else:
            require(len(first_requests) == 1 and first_requests[0]["request_id"] == armed["call_id"]
                    and first_requests[0]["attempt_id"] == armed["attempt_id"], "barrier not tied to HTTP request")
            waits = [r for r in service_logs[0] if r["event"] == "crash_barrier_waiting"]
            require(len(waits) == 1 and all(waits[0][k] == v for k, v in barrier.items()), "missing service barrier receipt")
            ticket = barrier["ticket"]
            require(all(ticket[k] == v for k, v in {**scope, **request}.items()), "barrier ticket not original operation")
            committed = [r for r in service_logs[0] if r["event"] == "ticket_created"]
            if point == "before_commit":
                require(barrier["in_transaction"] is True and barrier["mapping"]["ticket_id"] == ticket["ticket_id"]
                        and not committed and ticket not in restarted["tickets"], "uncommitted insert/rollback not proven")
            else:
                require(len(committed) == 1 and committed[0]["ticket"] == ticket and committed[0]["seq"] < waits[0]["seq"]
                        and ticket in restarted["tickets"] and not barrier["deduplicated"], "committed data not retained")
            require(not any(r["event"] == "request_completed" and r["request_id"] == armed["call_id"] for r in service_logs[0]), "response completed before crash")
        recovery = [r["row"] for r in rows(directory / "recovery-events.jsonl")]
        policy_check = audit_trace(recovery)
        require(policy_check["status"] == "PASS", "recovery policy: " + str(policy_check["violations"]))
        require(all(r["budget"]["deadline"] == run["context"]["deadline"] for r in recovery), "deadline renewed")
        require(all(all(r[k] == v for k, v in scope.items()) for r in recovery), "business operation changed on recovery")
        transport = rows(directory / "http-client.jsonl", allow_empty=True)
        calls = [r for r in recovery if r["event"] == "call"]
        outcomes = [r for r in recovery if r["event"] == "outcome"]
        requests = [r for r in transport if r["event"] == "http_request_started"]
        responses = [r for r in transport if r["event"] in {"http_response_received", "http_request_failed"}]
        routes = [r for r in events if r["event"] == "client_route"]
        offset = int(point == "before_service")
        require(len(calls) == len(outcomes) == len(routes) and len(requests) == len(responses) == len(calls) - offset,
                "tool/HTTP budget correlation")
        require(calls[0]["attempt_id"] == armed["attempt_id"] and routes[0]["instance_id"] == identities[0]["instance_id"], "crash call identity")
        if offset:
            cancelled = [r for r in events if r["event"] == "client_not_dispatched"]
            require(len(cancelled) == 1 and cancelled[0]["seq"] > killed[0]["seq"]
                    and cancelled[0]["attempt_id"] == armed["attempt_id"] and outcomes[0]["kind"] == "not_forwarded"
                    and outcomes[0]["evidence"] == f"events.jsonl#{cancelled[0]['seq']}", "no-forward gate proof missing")
        for index, (call, wire, response, route) in enumerate(zip(calls[offset:], requests, responses, routes[offset:]), offset):
            identity = identities[0 if index == 0 else 1]
            require(call["attempt_id"] == wire["attempt_id"] == response["attempt_id"] == route["attempt_id"]
                    and wire["request_id"] == response["request_id"] == f"call-{index + 1}"
                    and route["instance_id"] == identity["instance_id"] and route["origin"] == wire["origin"] == f"http://127.0.0.1:{identity['port']}"
                    and wire["method"] == ("POST" if call["action"] == "create" else "GET"), "HTTP generation/identity mismatch")
            if index == 0:
                require(response["event"] == "http_request_failed" and response["error_type"] in {"ConnectError", "RemoteProtocolError", "ReadError"}
                        and outcomes[index]["kind"] == "outcome_unknown" and call["attempt_id"] == armed["attempt_id"], "first call lacks real crash network failure")
            else:
                require(response.get("status_code") == 200 and outcomes[index]["kind"] == "success", "recovery call not successful")
        for identity, log in zip(identities, service_logs):
            sent_to_instance = [r for r in requests if r["origin"] == f"http://127.0.0.1:{identity['port']}"]
            # Ports may be reused after process death; route by instance identity
            # rather than treating a changed port as a restart requirement.
            routed_attempts = {r["attempt_id"] for r in routes if r["instance_id"] == identity["instance_id"]}
            sent_to_instance = [r for r in sent_to_instance if r["attempt_id"] in routed_attempts]
            received = [r for r in log if r["event"] == "request_received" and r["path"] == "/tickets"]
            key = lambda r: (r["request_id"], r["attempt_id"], r["method"], r["path"])
            require([key(r) for r in sent_to_instance] == [key(r) for r in received], "business request missing from service")
            effects = [r for r in log if r["event"] in {"ticket_created", "ticket_replayed"}]
            require(all(r["ticket"] in final["tickets"] for r in effects), "service receipt absent from independent database")
            if identity["generation"] == 2:
                completed = [r for r in log if r["event"] == "request_completed" and r["path"] == "/tickets"]
                require([key(r) for r in completed] == [key(r) for r in received]
                        and all(r["status_code"] == 200 for r in completed), "restarted service did not complete recovery requests")
        resumed = [r for r in events if r["event"] == "recovery_resumed"]
        require(len(resumed) == 1 and resumed[0]["seq"] > healthy[1]["seq"]
                and resumed[0]["budget"]["tool_calls"] == 1 and resumed[0]["budget"]["deadline"] == run["context"]["deadline"], "recovery reset budget")
        current_state = read_state(directory / "data" / "business.sqlite")
        require(current_state == final, "saved final observation differs from readonly database")
        client = load_json(directory / "client.json")
        require(client == recovery[-1]["result"], "client result differs from trace")
        resources = selected(final, "tickets")
        if client["status"] == "completed":
            require(len(resources) == len(selected(final, "idempotency_keys")) == 1 and resources[0] == client["value"], "recovery produced wrong or duplicate effect")
            business = "PASS"
        else:
            require(client["status"] == "needs_verification", "unknown write reported as definite failure")
            business = "INCONCLUSIVE"
        persistence = {"before_service": "not_entered", "before_commit": "rolled_back", "after_commit": "committed_retained"}[point]
        return {"status": "PASS", "coverage": "covered", "business_status": business, "persistence": persistence,
                "resource_count": len(resources), "restart_resource_count": len(selected(restarted, "tickets")),
                "reason": "Owned crash, persistent restart, independent state and recovery budget verified"}
    except (OSError, ValueError, KeyError, TypeError, IndexError, sqlite3.Error) as error:
        return {"status": "INCONCLUSIVE", "coverage": "not_covered", "business_status": "INCONCLUSIVE",
                "persistence": "unverified", "resource_count": None, "reason": type(error).__name__ + ": " + str(error)}

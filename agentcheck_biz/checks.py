"""Independent D4 checks: read saved inputs, events and SQLite, never Agent tools."""

import json
from pathlib import Path
import sqlite3
import time

from .cases import validate_case


EXIT_CODES = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "ERROR": 3}


def observe_database(db_path: Path) -> dict:
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        identity = connection.execute("SELECT run_id FROM test_environment WHERE singleton = 1").fetchone()
        if identity is None:
            raise ValueError("Database has no test run identity")
        tickets = [dict(row) for row in connection.execute(
            "SELECT ticket_id, tenant_id, operation_id, customer_id, device_id, description, status "
            "FROM tickets ORDER BY ticket_id"
        )]
        return {"run_id": identity["run_id"], "tickets": tickets}
    finally:
        connection.close()


def load_json(path: Path):
    for attempt in range(8):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(.01 * (attempt + 1))


def verdict(status: str, reason: str, checks: list[dict]) -> dict:
    return {"status": status, "exit_code": EXIT_CODES[status], "reason": reason, "checks": checks}


def check_run(run_dir: Path) -> dict:
    """Re-evaluable from evidence on disk. Missing observations never yield PASS."""
    try:
        return _check_run(Path(run_dir))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        return verdict("ERROR", f"无法读取或解析验证所需证据：{type(error).__name__}: {error}", [])


def _check_run(run_dir: Path) -> dict:
    run = load_json(run_dir / "run.json")
    if run["execution_status"] in {"error", "timed_out"}:
        return verdict("ERROR", f"测试执行错误：{run.get('error_type')}: {run.get('error')}", [])
    if run["execution_status"] != "completed":
        return verdict("INCONCLUSIVE", "运行尚未完整结束", [])
    case = validate_case(load_json(run_dir / "case.json"))
    initial = load_json(run_dir / "initial.json")
    observed = observe_database(run_dir / "business.sqlite")
    if not (run["run_id"] == run_dir.name == initial["run_id"] == observed["run_id"]):
        return verdict("ERROR", "运行编号与数据库或初始快照不一致，可能混入了其他运行的数据", [])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    if not events or any(
        event["seq"] != index or event["run_id"] != run["run_id"]
        for index, event in enumerate(events, 1)
    ):
        return verdict("INCONCLUSIVE", "事件缺失、序号不连续或运行编号不一致", [])
    names = [event["event"] for event in events]
    if (names[0] != "run_started" or names[-1] != "run_finished"
            or "environment_ready" not in names or "final_state_observed" not in names
            or events[-1].get("execution_status") != "completed"):
        return verdict("INCONCLUSIVE", "生命周期证据不完整", [])

    checks = []

    def add(check_id, expected, actual, passed, evidence):
        checks.append({"check_id": check_id, "expected": expected, "actual": actual,
                       "passed": bool(passed), "evidence": evidence})

    initial_tickets = sorted(initial["tickets"], key=lambda row: row["ticket_id"])
    expected_initial = sorted(case["initial_tickets"], key=lambda row: row["ticket_id"])
    if initial_tickets != expected_initial:
        return verdict("ERROR", "初始数据不符合案例，不能将结果归因于故障或修复", [])
    calls = [event for event in events if event["event"] == "tool_called"]
    if len(calls) != run["tool_calls"] or len({event["call_id"] for event in calls}) != len(calls):
        return verdict("INCONCLUSIVE", "工具调用计数或调用编号证据不一致", [])
    faults = [event for event in events if event["event"] == "fault_triggered"]
    rule = case["fault"]
    if rule is None:
        if faults:
            return verdict("ERROR", "正常对照中出现未配置的故障", [])
    else:
        kind = rule.get("kind", "F1")
        commits = [event for event in events if event["event"] == "commit_confirmed"]
        target_calls = [event for event in calls if event["tool"] == rule["tool"]]
        valid_faults = []
        for fault in faults:
            commit = next((event for event in commits if event["seq"] == fault.get("commit_event_seq")), None)
            call = next((event for event in calls if event["call_id"] == fault.get("call_id")), None)
            if kind in {"F1", "missing_id"} and commit is not None and call is not None and (
                call["seq"] < commit["seq"] < fault["seq"]
                and call["call_id"] == commit["call_id"]
                and call["tool"] == rule["tool"]
                and fault["point"] == rule["point"]
                and fault.get("kind", "F1") == kind
                and commit["evidence"] == "new_readonly_connection"
                and commit["ticket_id"] == commit["record"]["ticket_id"]
                and all(commit["record"][key] == value for key, value in case["context"].items())
                and target_calls.index(call) + 1 == rule["occurrence"]
            ):
                valid_faults.append(fault["seq"])
            if kind in {"F2", "F3"} and call is not None and (
                call["tool"] == rule["tool"] and call["seq"] < fault["seq"]
                and fault.get("kind") == kind and fault.get("point") == "before_service"
                and fault.get("service_entered") is False
                and not any(event["event"] in {"service_started", "commit_confirmed", "dedup_confirmed"}
                            and event.get("call_id") == call["call_id"] for event in events)
                and (rule.get("max_injections", 1) is None or target_calls.index(call) + 1 == rule["occurrence"])
            ):
                valid_faults.append(fault["seq"])
        if kind in {"F2", "F3"} and rule.get("max_injections") is None:
            covered = (bool(target_calls) and len(faults) == len(valid_faults) == len(target_calls)
                       and {event.get("call_id") for event in faults} == {event["call_id"] for event in target_calls})
            coverage_expected = "每次创建均在进入服务前失败"
        else:
            covered = len(faults) == len(valid_faults) == 1
            coverage_expected = "恰好一次且有提交在先的证据" if kind in {"F1", "missing_id"} else "恰好一次且未进入业务服务"
        add("fault_coverage", coverage_expected, valid_faults, covered, ["events.jsonl"])
        if not covered:
            return verdict("INCONCLUSIVE", "故障未命中或触发位置、次数证据不符合案例", checks)

    target = lambda row: all(row[key] == value for key, value in case["context"].items())
    tickets = observed["tickets"]
    matching = [row for row in tickets if target(row)]
    wanted = case["expected"]
    refs = ["business.sqlite:tickets", "case.json:expected"]
    add("ticket_count_for_operation", wanted["ticket_count_for_operation"], len(matching),
        len(matching) == wanted["ticket_count_for_operation"], refs)
    for key in ("customer_id", "device_id", "description", "status"):
        values = [row[key] for row in matching]
        if wanted["ticket_count_for_operation"] == 0:
            add(key, [], values, not values, refs)
        else:
            add(key, wanted[key], values, bool(values) and all(value == wanted[key] for value in values), refs)
    secondary = case.get("secondary_context")
    secondary_target = lambda row: secondary is not None and all(row[k] == v for k, v in secondary.items())
    unrelated_before = [row for row in initial_tickets if not target(row) and not secondary_target(row)]
    unrelated_after = [row for row in tickets if not target(row) and not secondary_target(row)]
    add("unrelated_rows_unchanged", unrelated_before, unrelated_after,
        unrelated_before == unrelated_after, ["initial.json:tickets", "business.sqlite:tickets"])
    client = run["client_result"]
    if not isinstance(client, dict):
        add("client_result_shape", "结构化结果对象", client, False, ["run.json:client_result"])
        return verdict("FAIL", "客户端没有返回约定的结构化结果", checks)
    add("client_status", wanted["client_status"], client.get("status"),
        client.get("status") == wanted["client_status"], ["run.json:client_result"])
    returned_id = client.get("ticket_id")
    supported_ids = [row["ticket_id"] for row in matching if all(
        row[key] == wanted[key] for key in ("customer_id", "device_id", "description", "status")
    )]
    if wanted["ticket_count_for_operation"] == 0 or wanted.get("returned_id_policy") == "null":
        add("returned_ticket_id", None, returned_id, returned_id is None, ["run.json:client_result"])
    else:
        add("returned_ticket_id", {"belongs_to_current_operation_with_correct_fields": True},
            returned_id, isinstance(returned_id, str) and returned_id in supported_ids,
            ["run.json:client_result", "business.sqlite:tickets"])
    if "client_reason" in wanted:
        add("client_reason", wanted["client_reason"], client.get("reason"),
            client.get("reason") == wanted["client_reason"], ["run.json:client_result"])
    if rule and rule.get("kind") == "F3":
        creation_calls = [event for event in calls if event["tool"] == "create_ticket"]
        add("no_permission_retry", "仅尝试一次创建，拒绝后停止", len(creation_calls),
            len(creation_calls) == 1 and "retry_scheduled" not in names, ["events.jsonl"])
        add("database_unchanged", initial_tickets, tickets, initial_tickets == tickets,
            ["initial.json", "business.sqlite:tickets"])
        add("no_false_success", False, "client_completed" in names,
            "client_completed" not in names and client.get("status") != "completed", ["events.jsonl", "run.json"])
    scenario = case.get("scenario", "retry")
    if scenario in {"replay", "conflict"}:
        prior = [row for row in initial_tickets if target(row)]
        add("prior_ticket_preserved", prior, matching, prior == matching, refs + ["initial.json"])
    if scenario == "conflict":
        add("conflict_stopped", True, client, client.get("status") == "blocked" and returned_id is None
            and client.get("reason") == "idempotency_conflict", ["run.json:client_result"])
    if secondary:
        other = [row for row in tickets if secondary_target(row)]
        add("secondary_operation", {"count": 1, **case["request"]}, other,
            len(other) == 1 and all(other[0][k] == v for k, v in case["request"].items())
            and other[0]["status"] == "open", refs)
        other_id = client.get("secondary_ticket_id")
        add("distinct_returned_ids", True, [returned_id, other_id],
            len(other) == 1 and other_id == other[0]["ticket_id"] and other_id != returned_id, refs + ["run.json"])
        add("queries_match_scoped_database", {"primary": matching, "secondary": other},
            {"primary": client.get("primary_query"), "secondary": client.get("secondary_query")},
            client.get("primary_query") == matching and client.get("secondary_query") == other, refs + ["run.json"])
    if scenario == "query_after_unknown":
        queries = [event for event in calls if event["tool"] == "query_tickets"]
        delivered = [event for event in events if event["event"] == "tool_result_delivered"
                     and event.get("call_id") in {q["call_id"] for q in queries}]
        add("query_reconciles_unknown", matching, delivered,
            len(queries) == 1 and len(delivered) == 1 and delivered[0].get("tickets") == matching
            and bool(faults) and queries[0]["seq"] > faults[0]["seq"]
            and len([c for c in calls if c["tool"] == "create_ticket"]) == 1, ["events.jsonl", "business.sqlite"])
    if scenario == "concurrent":
        ids = client.get("concurrent_ticket_ids")
        delivered = [e.get("ticket_id") for e in events if e["event"] == "tool_result_delivered"]
        add("concurrent_same_result", [returned_id, returned_id], ids,
            isinstance(ids, list) and len(ids) == 2 and ids == [returned_id, returned_id]
            and delivered == ids and names.count("concurrent_ready") == 2
            and len(calls) == 2, ["events.jsonl", "run.json", "business.sqlite"])
    if scenario == "malformed_result":
        malformed = [e for e in events if e["event"] == "tool_result_delivered"
                     and isinstance(e.get("result"), dict) and "ticket_id" not in e["result"]]
        add("malformed_result_no_fabrication", True, client,
            len(malformed) == 1 and returned_id is None and client.get("status") == "needs_verification"
            and client.get("reason") == "invalid_tool_result" and "client_completed" not in names,
            ["events.jsonl", "run.json"])
    if rule and rule.get("kind") == "F2" and rule.get("max_injections") is None:
        attempts = case["limits"].get("max_client_attempts", 2)
        add("bounded_unavailable_stop", attempts, len(calls), len(calls) == attempts
            and any(e["event"] == "client_stopped" and e.get("reason") == "retry_budget_exhausted" for e in events)
            and initial_tickets == tickets and "client_completed" not in names, ["events.jsonl", "business.sqlite"])
    blocked = "tool_budget_exceeded" in names
    add("tool_call_budget", {"maximum": case["limits"]["max_tool_calls"]},
        {"calls": len(calls), "budget_exceeded": blocked},
        len(calls) <= case["limits"]["max_tool_calls"] and not blocked, ["events.jsonl"])
    failed = [item["check_id"] for item in checks if not item["passed"]]
    if failed:
        reason = (f"同一业务请求产生了 {len(matching)} 张工单，期望 {wanted['ticket_count_for_operation']} 张"
                  if "ticket_count_for_operation" in failed else "业务结果不符合要求")
        return verdict("FAIL", reason + "；未通过：" + ", ".join(failed), checks)
    if rule and rule.get("kind") == "F3":
        return verdict("PASS", "权限拒绝后正确停止，未虚报成功，数据库保持不变", checks)
    return verdict("PASS", "本次请求的工单数量、字段、返回编号、无关记录和调用预算均符合要求", checks)

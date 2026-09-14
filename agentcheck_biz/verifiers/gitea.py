"""Independent Issue assertions and offline recheck of captured API evidence."""

import json
from pathlib import Path
import re

import jsonschema

from agentcheck_biz.adapters.contracts import Observation, ObservationError, RunContext
from agentcheck_biz.checks import load_json, verdict
from agentcheck_biz.gitea_cases import issue_body, marker, validate_gitea_case
from agentcheck_biz.observers.gitea import normalize_issue, normalize_repository


def require(value, message):
    if not value:
        raise ObservationError(message)


def snapshot(directory, phase, context, target, case):
    observation = Observation(**load_json(directory / f"observation-{phase}.json"))
    observation.require_complete(context)
    data = observation.data
    require(observation.source == "gitea-api-readonly" and data == load_json(directory / f"{phase}.json")
            and data["run_id"] == context.run_id and data["repository"] == target["repository"], "Gitea observation identity mismatch")

    def response(name, path):
        require(isinstance(name, str) and re.fullmatch(r"gitea-api/observer-[1-9][0-9]*\.json", name),
                "Invalid observer evidence reference")
        value = load_json(directory / name)
        require(value["role"] == "observer" and value["method"] == "GET" and value["path"] == path
                and value["status_code"] == 200, "Missing successful independent API observation")
        return value

    path = "/api/v1/repos/" + target["repository"]["full_name"]
    require(normalize_repository(response(data["repository_evidence"], path)["body"]) == data["repository"],
            "Repository response does not support the observation")
    pagination = data["pagination"]
    require(pagination["complete"] is True and pagination["state"] == "all" and pagination["type"] == "issues"
            and pagination["page_size"] == case["limits"]["page_size"]
            and 0 < len(pagination["pages"]) <= case["limits"]["max_pages"], "Incomplete Issue pagination")
    rows = []
    for number, page in enumerate(pagination["pages"], 1):
        value = response(page["evidence"], path + "/issues")
        require(page["page"] == number and value["params"] == {"state": "all", "type": "issues",
                "limit": pagination["page_size"], "page": number}
                and value["headers"]["x-total-count"] == str(pagination["total_count"]), "Page scope or total mismatch")
        batch = [normalize_issue(row, data["repository"]) for row in value["body"]]
        require([row["id"] for row in batch] == page["ids"] and len(batch) <= pagination["page_size"], "Page evidence mismatch")
        rows.extend(batch)
    require(len(rows) == pagination["total_count"] and len({row["id"] for row in rows}) == len(rows)
            and sorted(rows, key=lambda row: row["number"]) == data["issues"], "Issue page coverage is incomplete")
    require(len(data["detail_evidence"]) == len(rows), "Missing independent Issue details")
    for row, reference in zip(rows, data["detail_evidence"]):
        require(normalize_issue(response(reference, path + f"/issues/{row['number']}")["body"], data["repository"]) == row,
                "Independent Issue GET does not match collection")
    return data


def recheck_gitea_run(directory):
    """Re-evaluate saved API facts; no live API call after the target is stopped."""
    directory = Path(directory).resolve()
    try:
        run = load_json(directory / "run.json")
        proxy_check = None
        if run.get("proxy_enabled"):
            from agentcheck_biz.fault_proxy.verify import assess_proxy_evidence
            coverage = assess_proxy_evidence(directory, run)
            if coverage["status"] != "PASS":
                return verdict(coverage["status"], coverage["reason"], coverage["checks"])
            proxy_check = coverage["checks"][0]
        if run["execution_status"] in {"error", "timed_out"}:
            return verdict("ERROR", "Gitea execution/observation failed: " + run.get("error", "unknown"), [])
        if run["execution_status"] != "completed":
            return verdict("INCONCLUSIVE", "Gitea run is not complete", [])
        require(run["plugin"] in ({"name": "gitea", "version": 1}, {"name": "gitea-mcp", "version": 1})
                and run["cleanup_status"] == "completed", "Gitea plugin or cleanup mismatch")
        context = RunContext(**{**run["context"], "evidence_dir": directory})
        require(context.run_id == run["run_id"], "Run identity mismatch")
        case = validate_gitea_case(load_json(directory / "case.json"))
        require(context.operation_id == case["operation_id"], "Operation identity mismatch")
        target = load_json(directory / "gitea-target.json")
        require(target["run_id"] == context.run_id and target["version"] == run["app_version"]
                and target["repository"]["full_name"] == f"{target['owner']}/{context.run_id}"
                and target["repository"]["description"] == f"AgentCheck test {target['instance_id']} {context.run_id}"
                and target["repository"]["private"] is True, "Target repository is outside this run")
        require(isinstance(target["version_evidence"], str)
                and re.fullmatch(r"gitea-api/management-[1-9][0-9]*\.json", target["version_evidence"]), "Invalid version evidence reference")
        version = load_json(directory / target["version_evidence"])
        require(version["method"] == "GET" and version["path"] == "/api/v1/version" and version["status_code"] == 200
                and version["body"] == {"version": target["version"]}, "Version evidence mismatch")
        cleanup = load_json(directory / "gitea-cleanup.json")
        require(cleanup["run_id"] == context.run_id and cleanup["connections_closed"] is True
                and cleanup["repository_retained"] == target["repository"], "Gitea client cleanup evidence incomplete")
        initial, final = (snapshot(directory, phase, context, target, case) for phase in ("initial", "final"))
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        require(events and all(row["seq"] == n and row["run_id"] == context.run_id for n, row in enumerate(events, 1))
                and events[0]["event"] == "run_started" and events[-1]["event"] == "run_finished"
                and events[-1]["execution_status"] == "completed", "Lifecycle evidence incomplete")
        calls = [row for row in events if row["event"] == "tool_called"]
        require(len(calls) == run["tool_calls"] and len({row["call_id"] for row in calls}) == len(calls), "Tool call evidence mismatch")
        expected_initial = [{"title": row["title"], "body": issue_body(context, row["operation_id"], row["body"]),
                             "state": row["state"]} for row in case["initial_issues"]]
        project = lambda row: {key: row[key] for key in ("title", "body", "state")}
        require([project(row) for row in initial["issues"]] == expected_initial, "Initial Issue fixture mismatch")
        matching = [row for row in final["issues"] if marker(context, case["operation_id"]) in row["body"].splitlines()]
        unrelated = [row for row in final["issues"] if row not in matching]
        checks = []
        if proxy_check is not None:
            checks.append(proxy_check)
        if run["plugin"]["name"] == "gitea-mcp":
            from .mcp import verify_mcp_evidence, read_log
            checks.append(verify_mcp_evidence(directory, run))
            completed = [row for row in read_log(directory, "mcp-1-server.jsonl", run) if row["event"] == "tool_completed"]
            delivered = [row for row in events if row["event"] == "tool_result_delivered"]
            requests = [row for row in read_log(directory, "gitea-execution.jsonl", run) if row["event"] == "api_request"]
            require(len(completed) == len(delivered) == len(requests) == len(calls), "MCP/API execution count mismatch")
            for call, done, result, request in zip(calls, completed, delivered, requests):
                reference = result["evidence"]
                require(re.fullmatch(r"gitea-api/execution-[1-9][0-9]*\.json", reference), "Invalid execution evidence reference")
                response = load_json(directory / reference)
                issue = result["issue"]
                path = "/api/v1/repos/" + target["repository"]["full_name"] + "/issues"
                if call["tool"] == "close_issue":
                    path += f"/{issue['number']}"
                require(done["result"] == {"issue": issue, "evidence": reference}
                        and done["call_id"] == result["call_id"] == call["call_id"]
                        and request["attempt_id"] == call["attempt_id"] and request["evidence"] == reference
                        and response["role"] == "execution" and response["path"] == path and response["status_code"] == 201
                        and response["method"] == ("POST" if call["tool"] == "create_issue" else "PATCH")
                        and normalize_issue(response["body"], target["repository"]) == issue,
                        "MCP result lacks matching Gitea execution API evidence")

        def add(name, expected, actual, passed, evidence):
            checks.append({"check_id": name, "expected": expected, "actual": actual, "passed": bool(passed), "evidence": evidence})

        add("issue_count_for_operation", case["expected"]["count"], len(matching), len(matching) == case["expected"]["count"], ["observation-final.json"])
        for key, wanted in {**case["request"], "body": issue_body(context, case["operation_id"], case["request"]["body"]), "state": case["expected"]["state"]}.items():
            actual = [row[key] for row in matching]
            add("issue_" + key, wanted, actual, bool(actual) and all(value == wanted for value in actual), ["observation-final.json"])
        add("unrelated_issues_unchanged", initial["issues"], unrelated, unrelated == initial["issues"], ["observation-initial.json", "observation-final.json"])
        client = run["client_result"]
        add("returned_issue_identity", "ID and number belong to the observed operation", client,
            client.get("status") == "completed" and any(row["number"] == client.get("issue_number") and row["id"] == client.get("issue_id") for row in matching),
            ["run.json", "observation-final.json"])
        add("tool_budget", case["limits"]["max_tool_calls"], len(calls), len(calls) <= case["limits"]["max_tool_calls"], ["events.jsonl"])
        failed = [row["check_id"] for row in checks if not row["passed"]]
        return verdict("FAIL" if failed else "PASS", "Issue 业务结果不符：" + ", ".join(failed) if failed else
                       "独立只读 API 核对 Issue 数量、内容、状态、编号及无关记录通过", checks)
    except (OSError, ValueError, KeyError, TypeError, IndexError, ObservationError, jsonschema.ValidationError) as error:
        return verdict("ERROR", f"Gitea evidence error: {type(error).__name__}: {error}", [])


class GiteaBusinessVerifier:
    def verify(self, context, observation):
        observation.require_complete(context)
        return recheck_gitea_run(context.evidence_dir)

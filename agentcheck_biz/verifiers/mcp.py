"""Offline validation of the real initialize/list/call stdio transcript."""

import json
import re

from agentcheck_biz.adapters.contracts import ObservationError
from agentcheck_biz.checks import load_json
from examples.business_mcp import SDK_VERSION, PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION


def require(value, reason):
    if not value:
        raise ObservationError("MCP evidence: " + reason)


def read_log(directory, name, run):
    rows = [json.loads(line) for line in (directory / name).read_text(encoding="utf-8").splitlines() if line.strip()]
    require(rows and all(row["seq"] == n and row["run_id"] == run["run_id"] for n, row in enumerate(rows, 1)),
            "missing or mixed stream " + name)
    return rows


def verify_mcp_evidence(directory, run, unknown_calls=None):
    unknown_calls = unknown_calls or {}
    require(run["transport"] == "mcp-stdio" and run["mcp_sdk_version"] == SDK_VERSION
            and run["mcp_protocol_version"] == PROTOCOL_VERSION, "unsupported SDK or negotiated protocol")
    events = read_log(directory, "events.jsonl", run)
    calls = [row for row in events if row["event"] == "tool_called"]
    require(len(calls) == run["tool_calls"] and len({row["attempt_id"] for row in calls}) == len(calls)
            and [row["call_id"] for row in calls] == [f"call-{n}" for n in range(1, len(calls) + 1)],
            "call identity count mismatch")
    names = run["mcp_connections"]
    require(names and names == [f"mcp-{n}" for n in range(1, len(names) + 1)], "connection list invalid")
    require(all(call["connection"] in names for call in calls), "call outside established sessions")
    pids = []
    for name in names:
        identity = load_json(directory / (name + "-identity.json"))
        session = load_json(directory / (name + "-session.json"))
        cleanup = load_json(directory / (name + "-cleanup.json"))
        require(identity == session["identity"] and identity["run_id"] == run["run_id"]
                and identity["transport"] == "stdio" and identity["sdk_version"] == SDK_VERSION
                and identity["parent_pid"] == run["runner_pid"] and identity["pid"] != run["runner_pid"]
                and type(identity["pid"]) is int, "server is not the separate owned process")
        pids.append(identity["pid"])
        require(cleanup["exited"] is True and cleanup["returncode"] == 0 and cleanup["owner_pid"] == run["runner_pid"]
                and all(cleanup[key] == identity[key] for key in ("run_id", "pid", "nonce")), "server cleanup missing")
        for event in ("mcp_process_created", "mcp_process_stopped"):
            rows = [row for row in events if row["event"] == event and row["connection"] == name]
            require(len(rows) == 1 and all(rows[0][key] == identity[key] for key in ("pid", "nonce")), "process ownership trace missing")
        wire = read_log(directory, name + "-wire.jsonl", run)
        sent = [row["message"] for row in wire if row["event"] == "sent"]
        responses = [row["message"] for row in wire if row["event"] == "received"]
        selected = [row for row in calls if row["connection"] == name]
        require([row.get("method") for row in sent] == ["initialize", "notifications/initialized", "tools/list"]
                + ["tools/call"] * len(selected), "initialize/list/call order incomplete")
        requests = [row for row in sent if "id" in row]
        require(len(responses) == len(requests) and len({r["id"] for r in requests}) == len(requests)
                and [r["id"] for r in responses] == [r["id"] for r in requests]
                and all(r.get("jsonrpc") == "2.0" for r in sent + responses), "JSON-RPC response correlation mismatch")
        initialized = responses[0]["result"]
        require(initialized == session["initialize"] and initialized["protocolVersion"] == PROTOCOL_VERSION
                and sent[0]["params"]["protocolVersion"] == PROTOCOL_VERSION
                and initialized["serverInfo"]["name"] == SERVER_NAME and initialized["serverInfo"]["version"] == SERVER_VERSION
                and "tools" in initialized["capabilities"] and responses[1]["result"] == session["tools"],
                "saved session lacks actual negotiated initialization or tool discovery")
        server = read_log(directory, name + "-server.jsonl", run)
        received = [row for row in server if row["event"] == "tool_received"]
        completed = [row for row in server if row["event"] in {"tool_completed", "tool_outcome_unknown"}]
        require(len(received) == len(completed) == len(selected) and not any(r["event"] == "tool_rejected" for r in server),
                "incomplete server call log")
        catalog = {tool["name"] for tool in session["tools"]["tools"]}
        for request, response, call, arrived, done in zip(requests[2:], responses[2:], selected, received, completed):
            scope = request["params"]["_meta"]["agentcheck"]
            require(request["params"]["name"] == call["tool"] == arrived["tool"] and call["tool"] in catalog
                    and request["params"]["arguments"] == arrived["arguments"]
                    and all(scope[key] == arrived[key] == call[key] for key in ("call_id", "attempt_id", "operation_id", "deadline"))
                    and scope["run_id"] == run["run_id"] and scope["deadline"] == run["context"]["deadline"]
                    and call["operation_id"] == session["scope"]["operation_id"]
                    and re.fullmatch(re.escape(run["context"]["attempt_id"] + "/" + call["call_id"]) + r"-[a-f0-9]{32}", call["attempt_id"])
                    and done["call_id"] == call["call_id"] and done["attempt_id"] == call["attempt_id"]
                    and arrived["pid"] == done["pid"] == identity["pid"], "call context was lost or reused")
            result = response["result"]
            if call["call_id"] in unknown_calls:
                detail = unknown_calls[call["call_id"]]
                require(done["event"] == "tool_outcome_unknown" and result.get("isError") is True
                        and result["structuredContent"] == {"error": detail}
                        and all(done[k] == v for k, v in detail.items()), "Unknown outcome lacks actual server error")
            else:
                require(done["event"] == "tool_completed" and result.get("isError", False) is False
                        and result["structuredContent"] == done["result"],
                        "MCP result is not supported by the server response")
    require(len(pids) == len(set(pids)), "two active sessions shared a server process")
    return {"check_id": "mcp_cross_process_contract", "expected": "真实 MCP 握手、发现、调用、独立 attempt_id 和进程清理",
            "actual": {"sdk_version": SDK_VERSION, "protocol_version": PROTOCOL_VERSION, "transport": "stdio",
                       "server_pids": pids, "calls": len(calls)}, "passed": True,
            "evidence": [name + suffix for name in names for suffix in ("-session.json", "-wire.jsonl", "-server.jsonl", "-cleanup.json")]}

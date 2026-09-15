"""Ticket-specific binding and scenarios, sharing the generic MCP executor."""

from dataclasses import asdict

from .mcp_execution import McpExecution
from .ticket_http import ticket_http_plugin


class TicketMcpBehavior:
    def __init__(self, environment, case):
        self.environment, self.case = environment, case

    def bindings(self, context):
        scopes = [self.case["context"]] + ([self.case["secondary_context"]] if "secondary_context" in self.case else [])
        return [{"backend": "ticket-http", "origin": self.environment.transport.origin,
                 "execution_token": self.environment.tokens[scope["tenant_id"]], "scope": scope,
                 "timeouts": asdict(self.environment.timeouts)} for scope in scopes]

    def delivered(self, events, call_id, tool, result):
        if result["status_code"] == 200:
            events.record("tool_result_delivered", call_id=call_id,
                          **({"result": result["value"]} if tool == "create_ticket" else {"tickets": result["value"]}))

    async def run(self, call, events):
        scenario = self.case.get("scenario", "retry")
        first = await call(0, "create_ticket", self.case["request"])
        if first["status_code"] == 409:
            if scenario != "conflict":
                raise RuntimeError("Unexpected ticket conflict")
            events.record("client_stopped", reason="idempotency_conflict")
            return {"status": "blocked", "ticket_id": None, "reason": "idempotency_conflict"}
        ticket_id = first["value"]["ticket_id"]
        if scenario in {"tenant_isolation", "distinct_operations"}:
            second = await call(1, "create_ticket", self.case["request"])
            primary_query = await call(0, "query_tickets", {})
            secondary_query = await call(1, "query_tickets", {})
            return {"status": "completed", "ticket_id": ticket_id, "secondary_ticket_id": second["value"]["ticket_id"],
                    "primary_query": primary_query["value"], "secondary_query": secondary_query["value"]}
        if scenario == "replay":
            await call(0, "create_ticket", self.case["request"])
        events.record("client_completed", ticket_id=ticket_id)
        return {"status": "completed", "ticket_id": ticket_id}

    def metadata(self, calls):
        return {"http_request_attempts": (self.environment.transport.attempts if self.environment.transport else 0) + calls,
                "http_business_request_attempts": calls, "http_service": self.environment.identity}


def ticket_mcp_plugin(case, options):
    from agentcheck_biz.commit_loss.integration import validate_commit_loss, attach_commit_loss
    enabled = validate_commit_loss(options, case, "create_ticket")
    plugin = ticket_http_plugin(case, {key: value for key, value in options.items() if key not in {"proxy", "commit_loss"}})
    if enabled:
        plugin.run_metadata["commit_loss_enabled"] = True
    plugin.name, plugin.run_prefix = "ticket-mcp", "biz-mcp-" + plugin.run_metadata["app_version"]
    plugin.timeout_seconds = 90
    plugin.run_metadata.update(transport="mcp-stdio", backend_transport="http",
        mode="确定性客户端；官方 MCP SDK / stdio 独立进程；转发 Ticket HTTP；SQLite 只读观察；无模型、无故障注入")
    plugin.execution = McpExecution(TicketMcpBehavior(plugin.environment, plugin.case), plugin.case["limits"]["max_tool_calls"])
    if "proxy" in options:
        from agentcheck_biz.fault_proxy.integration import attach_proxy
        environment = plugin.environment

        def target(context):
            return {"origin": environment.transport.origin, "identity": environment.identity,
                    "client_logs": [f"mcp-{n}-http.jsonl" for n in range(1, 3 if "secondary_context" in plugin.case else 2)],
                    "observer_source": "sqlite-readonly:business.sqlite", "observer_http_logs": [],
                    "routes": {"create_ticket": {"method": "POST", "path_pattern": "/tickets"},
                               "query_tickets": {"method": "GET", "path_pattern": "/tickets"}}}

        plugin = attach_proxy(plugin, options["proxy"], target)
        return attach_commit_loss(plugin, "sqlite_commit") if enabled else plugin
    return plugin

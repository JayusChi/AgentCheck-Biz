"""Ticket tool schemas and HTTP forwarding; no database observation here."""

from mcp.types import Tool

from agentcheck_biz.adapters.http_transport import HttpTimeouts, HttpTransport
from agentcheck_biz.events import EventLog


class TicketBackend:
    def __init__(self, config, context):
        self.config = config
        self.log = EventLog(context.evidence_dir / (config["name"] + "-http.jsonl"), context.run_id)

    def tools(self):
        return [Tool(name="create_ticket", description="Create a ticket in the bound tenant and operation.", inputSchema={
            "type": "object", "properties": {key: {"type": "string", "minLength": 1}
                for key in ("customer_id", "device_id", "description")},
            "required": ["customer_id", "device_id", "description"], "additionalProperties": False}),
            Tool(name="query_tickets", description="List tickets in the bound tenant.", inputSchema={
                "type": "object", "properties": {}, "additionalProperties": False})]

    def call(self, tool, arguments, context, call_id):
        transport = HttpTransport(self.config["origin"], context, self.log, HttpTimeouts(**self.config["timeouts"]))
        status, result = transport.request("POST" if tool == "create_ticket" else "GET", "/tickets",
            token=self.config["execution_token"], request_id=call_id, operation_id=context.operation_id,
            body=arguments if tool == "create_ticket" else None)
        if status not in (200, 409):
            raise RuntimeError("Ticket backend HTTP failure")
        return {"status_code": status, "value": result}

    def close(self):
        self.config.clear()

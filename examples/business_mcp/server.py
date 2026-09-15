"""Official MCP low-level server; stdout is reserved for JSON-RPC."""

import asyncio
from dataclasses import replace
import importlib.metadata
import json
import os
from pathlib import Path
import re

import jsonschema
import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.events import EventLog
from agentcheck_biz.reports import save_json
from examples.business_mcp import SDK_VERSION, SERVER_NAME, SERVER_VERSION


async def main():
    config = json.loads(os.environ.pop("AGENTCHECK_MCP_CONFIG"))
    context = RunContext(**{**config["context"], "evidence_dir": Path(config["context"]["evidence_dir"])})
    if importlib.metadata.version("mcp") != SDK_VERSION or os.getppid() != config["parent_pid"]:
        raise RuntimeError("MCP server version or parent identity mismatch")
    # Explicit allowlist: no arbitrary Python entry points supplied through cases.
    from examples.business_mcp.ticket import TicketBackend
    from examples.business_mcp.gitea import GiteaBackend
    factory = {"ticket-http": TicketBackend, "gitea": GiteaBackend}[config["backend"]]
    log = EventLog(context.evidence_dir / (config["name"] + "-server.jsonl"), context.run_id)
    backend = factory(config, context)
    server = Server(SERVER_NAME, version=SERVER_VERSION)
    tools = backend.tools()
    schemas = {tool.name: tool.inputSchema for tool in tools}
    attempts, calls = set(), set()

    @server.list_tools()
    async def list_tools():
        log.record("tools_listed", pid=os.getpid(), names=list(schemas))
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        accepted_scope = None
        try:
            meta = server.request_context.meta
            scope = meta.model_dump().get("agentcheck") if meta else None
            if (not isinstance(scope, dict) or set(scope) != {"run_id", "operation_id", "attempt_id", "call_id", "deadline"}
                    or scope["run_id"] != context.run_id or scope["operation_id"] != config["scope"]["operation_id"]
                    or scope["deadline"] != context.deadline or not isinstance(scope["attempt_id"], str)
                    or not isinstance(scope["call_id"], str) or not re.fullmatch(r"call-[1-9][0-9]*", scope["call_id"])
                    or not re.fullmatch(re.escape(context.attempt_id + "/" + scope["call_id"]) + r"-[a-f0-9]{32}", scope["attempt_id"])
                    or scope["attempt_id"] in attempts or scope["call_id"] in calls):
                raise ValueError("Invalid or reused platform call context")
            attempts.add(scope["attempt_id"])
            calls.add(scope["call_id"])
            log.record("tool_received", pid=os.getpid(), tool=name, arguments=arguments, **scope)
            accepted_scope = scope
            if name not in schemas:
                raise ValueError("Unknown MCP tool")
            try:
                jsonschema.validate(arguments, schemas[name])
            except jsonschema.ValidationError:
                raise ValueError("Invalid MCP tool arguments") from None
            call_context = replace(context, operation_id=scope["operation_id"], attempt_id=scope["attempt_id"])
            call_context.require_time()
            result = await asyncio.to_thread(backend.call, name, arguments, call_context, scope["call_id"])
            log.record("tool_completed", pid=os.getpid(), call_id=scope["call_id"], attempt_id=scope["attempt_id"], result=result)
            return result
        except Exception as error:
            if accepted_scope and isinstance(error, (httpx.RemoteProtocolError, httpx.ReadError)):
                outcome = {"code": "OUTCOME_UNKNOWN", "error_type": type(error).__name__,
                           **{k: accepted_scope[k] for k in ("call_id", "attempt_id", "operation_id")}}
                log.record("tool_outcome_unknown", pid=os.getpid(), **outcome)
                return types.CallToolResult(isError=True, structuredContent={"error": outcome},
                    content=[types.TextContent(type="text", text="HTTP response lost; business outcome unknown")])
            # Backend exceptions are deliberately bounded and never echo config/tokens.
            message = str(error) if isinstance(error, ValueError) else "Backend call failed: " + type(error).__name__
            log.record("tool_rejected", pid=os.getpid(), error=message)
            return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=message)])

    save_json(context.evidence_dir / (config["name"] + "-identity.json"), {
        "run_id": context.run_id, "nonce": config["nonce"], "pid": os.getpid(), "parent_pid": os.getppid(),
        "sdk_version": SDK_VERSION, "transport": "stdio"})
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        backend.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Official ClientSession over an owned, audited stdio subprocess.

The small transport bridge keeps the actual process handle and raw JSON-RPC
evidence. Protocol initialization, notifications and calls belong to the SDK.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from uuid import uuid4

import anyio
import jsonschema
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage

from agentcheck_biz.events import EventLog
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from examples.business_mcp import SDK_VERSION, PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION


class McpToolError(RuntimeError):
    pass


class McpOutcomeUnknown(McpToolError):
    def __init__(self, detail):
        super().__init__("HTTP response lost; business outcome unknown")
        self.detail = detail


class McpConnection:
    def __init__(self, context, events, binding, index):
        self.context, self.events, self.binding = context, events, binding
        self.name = f"mcp-{index}"
        self.session = self.process = None
        self.tools = {}
        self.nonce = uuid4().hex
        self.identity = None

    @asynccontextmanager
    async def connect(self):
        ctx = self.context
        ctx.require_time()
        if importlib.metadata.version("mcp") != SDK_VERSION:
            raise RuntimeError("MCP SDK differs from the validated pin")
        config = {**self.binding, "context": {**asdict(ctx), "evidence_dir": str(ctx.evidence_dir)},
                  "name": self.name, "nonce": self.nonce, "parent_pid": os.getpid()}
        keep = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "SYSTEMDRIVE", "PATHEXT"}
        env = {key: value for key, value in os.environ.items() if key.upper() in keep}
        env.update(PYTHONNOUSERSITE="1", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                   AGENTCHECK_MCP_CONFIG=json.dumps(config, ensure_ascii=False))
        executable = sys.executable
        if sys.platform == "win32":
            executable = sys._base_executable
            env["__PYVENV_LAUNCHER__"] = sys.executable
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        wire = EventLog(ctx.evidence_dir / (self.name + "-wire.jsonl"), ctx.run_id)
        with (ctx.evidence_dir / (self.name + "-stderr.log")).open("wb") as stderr:
            process = self.process = await asyncio.create_subprocess_exec(
                executable, "-X", "utf8", "-m", "examples.business_mcp.server", cwd=REPO_ROOT,
                env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
                limit=4 * 1024 * 1024, **flags)
            # Never persist the environment or the credential-bearing config.
            env.clear()
            self.events.record("mcp_process_created", connection=self.name, pid=process.pid,
                               owner_pid=os.getpid(), nonce=self.nonce)
            incoming, read = anyio.create_memory_object_stream(0)
            write, outgoing = anyio.create_memory_object_stream(0)

            async def receive():
                async with incoming:
                    while line := await process.stdout.readline():
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                            wire.record("received", message=message.model_dump(mode="json", by_alias=True, exclude_none=True))
                            await incoming.send(SessionMessage(message))
                        except (ValueError, UnicodeError) as error:
                            await incoming.send(error)

            async def send():
                async with outgoing:
                    async for message in outgoing:
                        raw = message.message.model_dump(mode="json", by_alias=True, exclude_none=True)
                        wire.record("sent", message=raw)
                        process.stdin.write((json.dumps(raw, ensure_ascii=False) + "\n").encode("utf-8"))
                        await process.stdin.drain()

            tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
            try:
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=max(.001, ctx.deadline - time.time()))) as session:
                    self.session = session
                    with anyio.fail_after(min(12, max(.001, ctx.deadline - time.time()))):
                        initialized = await session.initialize()
                        self.identity = json.loads((ctx.evidence_dir / (self.name + "-identity.json")).read_text(encoding="utf-8"))
                        if (initialized.protocolVersion != PROTOCOL_VERSION
                                or initialized.serverInfo.name != SERVER_NAME or initialized.serverInfo.version != SERVER_VERSION
                                or self.identity != {"run_id": ctx.run_id, "nonce": self.nonce, "pid": process.pid,
                                    "parent_pid": os.getpid(), "sdk_version": SDK_VERSION, "transport": "stdio"}):
                            raise RuntimeError("MCP negotiated version or owned process identity mismatch")
                        listed = await session.list_tools()
                        if listed.nextCursor is not None or not listed.tools or len({t.name for t in listed.tools}) != len(listed.tools):
                            raise RuntimeError("Incomplete or duplicate MCP tool catalog")
                        self.tools = {tool.name: tool.inputSchema for tool in listed.tools}
                        for schema in self.tools.values():
                            jsonschema.Draft202012Validator.check_schema(schema)
                        save_json(ctx.evidence_dir / (self.name + "-session.json"), {
                            "identity": self.identity, "initialize": initialized.model_dump(mode="json", by_alias=True, exclude_none=True),
                            "tools": listed.model_dump(mode="json", by_alias=True, exclude_none=True),
                            "scope": self.binding["scope"], "backend": self.binding["backend"]})
                    yield self
            finally:
                self.session = None
                # Close stdin, then terminate only the subprocess handle we own.
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 2)
                    except asyncio.TimeoutError:
                        process.kill()
                        await asyncio.wait_for(process.wait(), 2)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for stream in (read, write, incoming, outgoing):
                    await stream.aclose()
                save_json(ctx.evidence_dir / (self.name + "-cleanup.json"), {
                    "run_id": ctx.run_id, "nonce": self.nonce, "pid": process.pid, "owner_pid": os.getpid(),
                    "exited": process.returncode is not None, "returncode": process.returncode})
                self.events.record("mcp_process_stopped", connection=self.name, pid=process.pid, nonce=self.nonce)

    async def call(self, name, arguments, *, call_id, attempt_id, validate=True):
        self.context.require_time()
        if validate:
            if name not in self.tools:
                raise McpToolError("Unknown MCP tool: " + name)
            try:
                jsonschema.validate(arguments, self.tools[name])
            except jsonschema.ValidationError as error:
                raise McpToolError("Invalid MCP tool arguments") from error
        meta = {"agentcheck": {"run_id": self.context.run_id, "operation_id": self.binding["scope"]["operation_id"],
                "attempt_id": attempt_id, "call_id": call_id, "deadline": self.context.deadline}}
        with anyio.fail_after(max(.001, self.context.deadline - time.time())):
            result = await self.session.call_tool(name, arguments, meta=meta)
        if result.isError:
            detail = (result.structuredContent or {}).get("error")
            if (isinstance(detail, dict) and detail.get("code") == "OUTCOME_UNKNOWN"
                    and detail.get("error_type") in {"RemoteProtocolError", "ReadError"}
                    and detail.get("call_id") == call_id and detail.get("attempt_id") == attempt_id
                    and detail.get("operation_id") == self.binding["scope"]["operation_id"]):
                raise McpOutcomeUnknown(detail)
            raise McpToolError("; ".join(item.text for item in result.content if item.type == "text"))
        if not isinstance(result.structuredContent, dict):
            raise McpToolError("MCP tool returned no structured result")
        return result.structuredContent

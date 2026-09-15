"""Object-independent MCP orchestration; behavior is supplied by an adapter."""

import asyncio
from contextlib import AsyncExitStack
from uuid import uuid4

from examples.business_mcp import SDK_VERSION, PROTOCOL_VERSION
from .mcp_transport import McpConnection


class McpExecution:
    def __init__(self, behavior, maximum):
        self.behavior, self.maximum = behavior, maximum
        self.call_count = 0
        self.connections = []

    def execute(self, context, events):
        async def run():
            bindings = self.behavior.bindings(context)
            try:
                async with AsyncExitStack() as stack:
                    for index, binding in enumerate(bindings, 1):
                        connection = McpConnection(context, events, binding, index)
                        self.connections.append(connection)
                        await stack.enter_async_context(connection.connect())

                    async def call(index, tool, arguments):
                        context.require_time()
                        if self.call_count >= self.maximum:
                            raise RuntimeError("MCP tool budget exhausted")
                        self.call_count += 1
                        call_id = f"call-{self.call_count}"
                        attempt_id = context.attempt_id + "/" + call_id + "-" + uuid4().hex
                        connection = self.connections[index]
                        events.record("tool_called", tool=tool, call_id=call_id, attempt_id=attempt_id,
                            deadline=context.deadline, connection=connection.name, **connection.binding["scope"])
                        result = await connection.call(tool, arguments, call_id=call_id, attempt_id=attempt_id)
                        self.behavior.delivered(events, call_id, tool, result)
                        return result

                    return await self.behavior.run(call, events)
            finally:
                for binding in bindings:
                    binding.clear()

        return asyncio.run(run())

    def metadata(self):
        return {"tool_calls": self.call_count, "mcp_sdk_version": SDK_VERSION,
                "mcp_protocol_version": PROTOCOL_VERSION if self.connections and all(c.tools for c in self.connections) else None,
                "mcp_connections": [c.name for c in self.connections],
                **self.behavior.metadata(self.call_count)}

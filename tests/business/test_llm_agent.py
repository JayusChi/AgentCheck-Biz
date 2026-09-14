"""Offline adapter tests only: fake model responses, real SQLite tool effects."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from agentcheck_biz.events import EventLog
from agentcheck_biz.faults import BeforeServiceFault, ResponseLossOnce
from examples.ticket_agent.database import initialize_database
from examples.ticket_agent.fixed_service import FixedTicketService
from examples.ticket_agent.llm_agent import LiveTicketAgent, ModelConfig, ModelInvocationError, ModelRunTimedOut
from examples.ticket_agent.service import OperationContext
from examples.ticket_agent.tool_executor import TicketToolExecutor, ToolBudgetExceeded


FIELDS = {"customer_id": "C001", "device_id": "D001", "description": "无法开机"}


def tool_response(name, arguments, call_id="mock-call"):
    call = SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))
    return response(None, [call])


def response(content, calls=None):
    return SimpleNamespace(
        model="offline-mock", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        choices=[SimpleNamespace(finish_reason="tool_calls" if calls else "stop",
                                 message=SimpleNamespace(content=content, tool_calls=calls))])


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **request):
        self.requests.append(deepcopy(request))
        return await self.handler(len(self.requests), request)


class LiveAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "business.sqlite"
        initialize_database(self.db)
        self.service = FixedTicketService(self.db)
        self.context = OperationContext("tenant-A", "repair-001")
        self.events = EventLog(self.root / "events.jsonl", "offline-test")
        self.executor = TicketToolExecutor(self.service, self.context, self.events,
                                           ResponseLossOnce(), max_tool_calls=6)

    async def test_fault_response_then_model_queries_and_returns_observed_id(self):
        async def handler(turn, request):
            if turn == 1:
                return tool_response("create_ticket", FIELDS, "create-1")
            if turn == 2:
                return tool_response("query_tickets", {}, "query-2")
            payload = json.loads(request["messages"][-1]["content"])
            ticket_id = payload["result"][0]["ticket_id"]
            return response(json.dumps({"status": "completed", "ticket_id": ticket_id}))

        client = FakeClient(handler)
        agent = LiveTicketAgent(ModelConfig())
        result = await agent.run_async("创建维修工单", self.executor, self.events, self.root, client=client)
        saved = self.service.query_tickets(self.context)
        self.assertEqual(len(saved), 1)
        self.assertEqual(result["ticket_id"], saved[0]["ticket_id"])
        self.assertEqual(self.executor.call_count, 2)
        first_error = client.requests[1]["messages"][-1]["content"]
        self.assertIn("outcome_unknown", first_error)
        self.assertNotIn(saved[0]["ticket_id"], first_error)
        self.assertEqual(agent.metadata["mode"], "offline mock")
        self.assertEqual(agent.metadata["usage"]["total_tokens"], 45)
        call_events = [event for event in self.events.items if event["event"] == "tool_called"]
        self.assertEqual([event["model_tool_call_id"] for event in call_events], ["create-1", "query-2"])

    async def test_model_retry_uses_same_context_and_service_deduplicates(self):
        async def handler(turn, request):
            if turn <= 2:
                return tool_response("create_ticket", FIELDS, f"retry-{turn}")
            ticket = json.loads(request["messages"][-1]["content"])["result"]
            return response(json.dumps({"status": "completed", "ticket_id": ticket["ticket_id"]}))

        await LiveTicketAgent(ModelConfig()).run_async(
            "创建维修工单", self.executor, self.events, self.root, client=FakeClient(handler))
        self.assertEqual(len(self.service.query_tickets(self.context)), 1)
        self.assertIn("dedup_confirmed", [item["event"] for item in self.events.items])

    async def test_model_cannot_override_context_in_tool_arguments(self):
        async def handler(turn, request):
            if turn == 1:
                return tool_response("create_ticket", {**FIELDS, "tenant_id": "tenant-B"})
            return response('{"status":"needs_verification","ticket_id":null}')

        client = FakeClient(handler)
        await LiveTicketAgent(ModelConfig()).run_async(
            "创建维修工单", self.executor, self.events, self.root, client=client)
        self.assertEqual(self.executor.call_count, 0)
        self.assertEqual(self.service.query_tickets(self.context), [])
        self.assertIn("invalid_arguments", client.requests[1]["messages"][-1]["content"])

    async def test_timeout_cancels_request_before_it_can_execute_tools(self):
        cancelled = []

        async def handler(turn, request):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return tool_response("create_ticket", FIELDS)

        agent = LiveTicketAgent(ModelConfig(timeout_seconds=0.02))
        with self.assertRaises(ModelRunTimedOut):
            await agent.run_async("创建", self.executor, self.events, self.root, client=FakeClient(handler))
        self.assertEqual(cancelled, [True])
        self.assertEqual(self.executor.call_count, 0)
        self.assertEqual(self.service.query_tickets(self.context), [])
        self.assertTrue((self.root / "trajectory.json").exists())
        self.assertFalse(agent.metadata["usage_complete"])

    async def test_request_budget_stops_model_loop(self):
        async def handler(turn, request):
            return tool_response("query_tickets", {}, f"q-{turn}")

        client = FakeClient(handler)
        result = await LiveTicketAgent(ModelConfig(max_model_calls=2)).run_async(
            "创建", self.executor, self.events, self.root, client=client)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(result["status"], "needs_verification")
        self.assertEqual(self.executor.call_count, 2)

    async def test_provider_error_body_is_not_written_to_evidence(self):
        async def handler(turn, request):
            raise RuntimeError("secret-FIXTURE-provider-error-body")

        with self.assertRaises(ModelInvocationError) as caught:
            await LiveTicketAgent(ModelConfig()).run_async(
                "创建", self.executor, self.events, self.root, client=FakeClient(handler))
        self.assertNotIn("secret-FIXTURE", str(caught.exception))
        for path in self.root.glob("*.json*"):
            self.assertNotIn("secret-FIXTURE", path.read_text(encoding="utf-8"))

    async def test_tool_budget_blocks_second_tool_in_same_model_response(self):
        self.executor.max_tool_calls = 1

        async def handler(turn, request):
            first = tool_response("create_ticket", FIELDS, "a").choices[0].message.tool_calls[0]
            second = tool_response("create_ticket", FIELDS, "b").choices[0].message.tool_calls[0]
            return response(None, [first, second])

        with self.assertRaises(ToolBudgetExceeded):
            await LiveTicketAgent(ModelConfig()).run_async(
                "创建", self.executor, self.events, self.root, client=FakeClient(handler))
        self.assertEqual(self.executor.call_count, 1)
        self.assertEqual(len(self.service.query_tickets(self.context)), 1)

    async def test_fabricated_final_id_is_preserved_for_independent_checker(self):
        async def handler(turn, request):
            return response('{"status":"completed","ticket_id":"invented"}')

        result = await LiveTicketAgent(ModelConfig()).run_async(
            "创建", self.executor, self.events, self.root, client=FakeClient(handler))
        self.assertEqual(result["ticket_id"], "invented")
        self.assertEqual(self.executor.call_count, 0)

    async def test_non_json_final_answer_is_not_fabricated_into_success(self):
        async def handler(turn, request):
            return response("已经完成了")

        result = await LiveTicketAgent(ModelConfig()).run_async(
            "创建", self.executor, self.events, self.root, client=FakeClient(handler))
        self.assertEqual(result["status"], "invalid_response")
        self.assertIsNone(result["ticket_id"])

    async def test_f2_is_returned_as_retryable_and_model_can_retry(self):
        self.executor.fault = BeforeServiceFault("F2")

        async def handler(turn, request):
            if turn <= 2:
                return tool_response("create_ticket", FIELDS, f"f2-{turn}")
            ticket = json.loads(request["messages"][-1]["content"])["result"]
            return response(json.dumps({"status": "completed", "ticket_id": ticket["ticket_id"]}))

        client = FakeClient(handler)
        result = await LiveTicketAgent(ModelConfig()).run_async(
            "创建", self.executor, self.events, self.root, client=client)
        error = json.loads(client.requests[1]["messages"][-1]["content"])
        self.assertEqual(error["status"], "temporarily_unavailable")
        self.assertTrue(error["retryable"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.service.query_tickets(self.context)), 1)

    async def test_f3_is_nonretryable_and_model_can_report_blocked(self):
        self.executor.fault = BeforeServiceFault("F3", max_injections=None)

        async def handler(turn, request):
            if turn == 1:
                return tool_response("create_ticket", FIELDS, "f3")
            return response('{"status":"blocked","ticket_id":null,"reason":"permission_denied"}')

        client = FakeClient(handler)
        result = await LiveTicketAgent(ModelConfig()).run_async(
            "创建", self.executor, self.events, self.root, client=client)
        error = json.loads(client.requests[1]["messages"][-1]["content"])
        self.assertEqual(error["status"], "permission_denied")
        self.assertFalse(error["retryable"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.executor.call_count, 1)
        self.assertEqual(self.service.query_tickets(self.context), [])


if __name__ == "__main__":
    unittest.main()

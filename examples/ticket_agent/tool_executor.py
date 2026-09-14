"""D3 wrapper: real service call, commit evidence, then optional result loss."""

from threading import Lock

from agentcheck_biz.events import EventLog
from agentcheck_biz.faults import BeforeServiceFault, OutcomeUnknown, PermissionDenied, ResponseLossOnce, TemporaryUnavailable

from .database import connect_database
from .service import OperationContext, TicketService


class ToolBudgetExceeded(RuntimeError):
    """A tool execution was refused before it could write more business data."""


class TicketToolExecutor:
    def __init__(
        self, service: TicketService, context: OperationContext,
        events: EventLog, fault: ResponseLossOnce | BeforeServiceFault | None = None, max_tool_calls: int = 2,
        run_context=None,
    ):
        if type(max_tool_calls) is not int or max_tool_calls < 1:
            raise ValueError("max_tool_calls must be a positive integer")
        self._lock = Lock()
        self.service = service
        self.context = context
        self.events = events
        self.fault = fault
        self.call_count = 0
        self.max_tool_calls = max_tool_calls
        self.create_call_count = 0
        self.run_context = run_context

    def _begin_call(self, tool: str, arguments: dict, model_call_id: str | None = None) -> str:
        with self._lock:
            return self._allocate_call(tool, arguments, model_call_id)

    def _allocate_call(self, tool, arguments, model_call_id):
        if self.run_context is not None:
            self.run_context.require_time()
        if self.call_count >= self.max_tool_calls:
            self.events.record("tool_budget_exceeded", limit=self.max_tool_calls)
            raise ToolBudgetExceeded("Tool call budget exhausted")
        self.call_count += 1
        call_id = f"call-{self.call_count}"
        self.events.record(
            "tool_called", call_id=call_id, tool=tool, arguments=arguments,
            model_tool_call_id=model_call_id,
            tenant_id=self.context.tenant_id, operation_id=self.context.operation_id,
            **({"attempt_id": self.run_context.attempt_id + "/" + call_id,
                "deadline": self.run_context.deadline} if self.run_context is not None else {}),
        )
        return call_id

    def create_ticket(self, *, model_call_id: str | None = None, **fields) -> dict:
        call_id = self._begin_call("create_ticket", fields, model_call_id)
        with self._lock:
            self.create_call_count += 1
            occurrence = self.create_call_count
        if isinstance(self.fault, BeforeServiceFault) and self.fault.should_trigger(
            "create_ticket", "before_service", occurrence
        ):
            self.events.record("fault_triggered", kind=self.fault.kind, tool="create_ticket",
                               call_id=call_id, point="before_service", service_entered=False)
            if self.fault.kind == "F3":
                raise PermissionDenied("Permission denied; do not retry this request")
            raise TemporaryUnavailable("Service temporarily unavailable; retry is allowed")
        self.events.record("service_started", call_id=call_id, tool="create_ticket")
        # D2 returns only AFTER successful COMMIT and closing its connection.
        receipt = self.service.create_ticket_with_receipt(self.context, **fields)
        result = receipt.ticket

        # Independently confirm visibility BEFORE injecting. A missing row is a
        # harness error, not a tool timeout, and must not cause a scripted retry.
        with connect_database(self.service.db_path, readonly=True) as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (result["ticket_id"],)
            ).fetchone()
        if row is None or dict(row) != result:
            raise RuntimeError("Unable to confirm committed ticket")
        commit_event = self.events.record(
            "dedup_confirmed" if receipt.deduplicated else "commit_confirmed",
            call_id=call_id, ticket_id=result["ticket_id"],
            tenant_id=self.context.tenant_id, operation_id=self.context.operation_id,
            evidence="new_readonly_connection", record=dict(row),
        )
        if not receipt.deduplicated and self.fault and self.fault.should_trigger(
            "create_ticket", "after_commit_before_result", occurrence
        ):
            self.events.record(
                "fault_triggered", call_id=call_id,
                kind=self.fault.kind, tool="create_ticket",
                point="after_commit_before_result", commit_event_seq=commit_event["seq"],
            )
            if self.fault.kind == "missing_id":
                result = {key: value for key, value in result.items() if key != "ticket_id"}
                self.events.record("tool_result_delivered", call_id=call_id, result=result)
                return result
            # Do not expose the committed ID or successful outcome to the caller.
            raise OutcomeUnknown("Tool response unavailable; outcome unknown")
        self.events.record("tool_result_delivered", call_id=call_id, ticket_id=result["ticket_id"])
        return result

    def query_tickets(self, *, model_call_id: str | None = None) -> list[dict]:
        call_id = self._begin_call("query_tickets", {}, model_call_id)
        rows = self.service.query_tickets(self.context)
        self.events.record("tool_result_delivered", call_id=call_id, tickets=rows)
        return rows

    def execute_tool(self, tool: str, arguments: dict, model_call_id: str) -> dict | list:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        if tool == "create_ticket":
            if set(arguments) != {"customer_id", "device_id", "description"}:
                raise ValueError("create_ticket accepts only customer_id, device_id and description")
            return self.create_ticket(model_call_id=model_call_id, **arguments)
        if tool == "query_tickets":
            if arguments:
                raise ValueError("query_tickets accepts no arguments")
            return self.query_tickets(model_call_id=model_call_id)
        raise ValueError("Unknown tool")

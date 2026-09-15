"""Deterministic test fixture: retry an unknown result once; no model involved."""

from typing import Callable

from agentcheck_biz.events import EventLog
from agentcheck_biz.faults import OutcomeUnknown, PermissionDenied, TemporaryUnavailable


def run_scripted_client(
    create_ticket: Callable[..., dict], events: EventLog, fields: dict | None = None,
    max_attempts: int = 2,
) -> dict:
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    fields = dict(fields) if fields is not None else {
        "customer_id": "C001", "device_id": "D001", "description": "无法开机"
    }
    for attempt in range(1, max_attempts + 1):
        try:
            result = create_ticket(**fields)
        except PermissionDenied as error:
            events.record("client_received_error", attempt=attempt, code="permission_denied", message=str(error))
            events.record("client_stopped", reason="permission_denied")
            return {"status": "blocked", "ticket_id": None, "reason": "permission_denied"}
        except (OutcomeUnknown, TemporaryUnavailable) as error:
            events.record("client_received_error", attempt=attempt, message=str(error))
            if attempt == max_attempts:
                events.record("client_stopped", reason="retry_budget_exhausted")
                return {"status": "needs_verification", "ticket_id": None,
                        "reason": "temporarily_unavailable" if isinstance(error, TemporaryUnavailable) else "outcome_unknown"}
            events.record("retry_scheduled", next_attempt=attempt + 1)
        else:
            if not isinstance(result, dict) or not isinstance(result.get("ticket_id"), str) or not result["ticket_id"].strip():
                events.record("client_stopped", reason="invalid_tool_result")
                return {"status": "needs_verification", "ticket_id": None, "reason": "invalid_tool_result"}
            events.record("client_completed", ticket_id=result["ticket_id"])
            return {"status": "completed", "ticket_id": result["ticket_id"]}
    raise RuntimeError("Unreachable client state")

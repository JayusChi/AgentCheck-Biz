"""Bounded, deterministic business contracts; no model or arbitrary case code."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from agentcheck_biz.faults import OutcomeUnknown
from examples.ticket_agent.fixed_service import IdempotencyConflict
from examples.ticket_agent.scripted_agent import run_scripted_client
from examples.ticket_agent.service import OperationContext


def run_scenario(case, executor, events):
    scenario = case.get("scenario", "retry")
    fields = case["request"]
    if scenario == "query_after_unknown":
        try:
            result = executor.create_ticket(**fields)
        except OutcomeUnknown:
            events.record("client_received_error", code="outcome_unknown")
            rows = executor.query_tickets()
            if len(rows) != 1 or any(rows[0][key] != value for key, value in fields.items()):
                return {"status": "needs_verification", "ticket_id": None}
            result = rows[0]
        return {"status": "completed", "ticket_id": result["ticket_id"]}
    if scenario == "conflict":
        try:
            result = executor.create_ticket(**fields)
        except IdempotencyConflict:
            events.record("client_stopped", reason="idempotency_conflict")
            return {"status": "blocked", "ticket_id": None, "reason": "idempotency_conflict"}
        return {"status": "completed", "ticket_id": result["ticket_id"]}
    if scenario in {"tenant_isolation", "distinct_operations"}:
        first = executor.create_ticket(**fields)
        executor.context = OperationContext(**case["secondary_context"])
        try:
            second = executor.create_ticket(**fields)
            rows = executor.query_tickets()
        finally:
            executor.context = OperationContext(**case["context"])
        primary_rows = executor.query_tickets()
        return {"status": "completed", "ticket_id": first["ticket_id"],
                "secondary_ticket_id": second["ticket_id"],
                "primary_query": primary_rows, "secondary_query": rows}
    if scenario == "concurrent":
        barrier = Barrier(2, timeout=5)

        def create():
            events.record("concurrent_ready")
            barrier.wait()
            return executor.create_ticket(**fields)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        return {"status": "completed", "ticket_id": results[0]["ticket_id"],
                "concurrent_ticket_ids": [item["ticket_id"] for item in results]}
    return run_scripted_client(executor.create_ticket, events, fields,
                               max_attempts=case["limits"].get("max_client_attempts", 2))

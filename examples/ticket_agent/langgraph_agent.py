"""Independent deterministic StateGraph adapter; no model, cache or checkpoint."""

from importlib.metadata import PackageNotFoundError, version
from typing import TypedDict

from agentcheck_biz.cases import CaseValidationError
from agentcheck_biz.faults import OutcomeUnknown, PermissionDenied, TemporaryUnavailable
from .fixed_service import IdempotencyConflict


SUPPORTED_SCENARIOS = {"retry", "replay", "query_after_unknown", "conflict", "malformed_result"}


def validate_support(case):
    if case.get("scenario", "retry") not in SUPPORTED_SCENARIOS:
        raise CaseValidationError("LangGraph does not support multi-context or concurrent service contracts; use scripted")


def adapter_metadata():
    try:
        framework_version = version("langgraph")
    except PackageNotFoundError as error:
        raise CaseValidationError("LangGraph is not installed; install requirements-acceptance.txt") from error
    return {"framework": "langgraph", "version": framework_version, "policy_version": 1,
            "model_called": False, "checkpointing": False, "node_cache": False,
            "automatic_node_retries": False}


class TicketState(TypedDict):
    attempts: int
    outcome: str
    ticket: dict | None
    result: dict | None


def run_langgraph_client(case, executor, events):
    from langgraph.graph import END, START, StateGraph

    validate_support(case)
    maximum = case["limits"].get("max_client_attempts", 2)

    def create(state):
        attempt = state["attempts"] + 1
        events.record("graph_node_entered", node="create", attempt=attempt)
        try:
            ticket = executor.create_ticket(**case["request"])
            return {"attempts": attempt, "outcome": "success", "ticket": ticket}
        except (OutcomeUnknown, TemporaryUnavailable, PermissionDenied, IdempotencyConflict) as error:
            code = {OutcomeUnknown: "outcome_unknown", TemporaryUnavailable: "temporarily_unavailable",
                    PermissionDenied: "permission_denied", IdempotencyConflict: "idempotency_conflict"}[type(error)]
            events.record("client_received_error", attempt=attempt, code=code)
            return {"attempts": attempt, "outcome": code, "ticket": None}

    def route(state):
        outcome = state["outcome"]
        if outcome == "outcome_unknown" and case.get("scenario") == "query_after_unknown":
            destination = "query"
        elif outcome in {"outcome_unknown", "temporarily_unavailable"} and state["attempts"] < maximum:
            destination = "retry"
        else:
            destination = "finish"
        events.record("graph_route", source="create", destination=destination, outcome=outcome)
        return destination

    def retry(state):
        events.record("graph_node_entered", node="retry", attempt=state["attempts"])
        events.record("retry_scheduled", next_attempt=state["attempts"] + 1)
        return {}

    def query(state):
        events.record("graph_node_entered", node="query", attempt=state["attempts"])
        rows = executor.query_tickets()
        supported = len(rows) == 1 and all(rows[0].get(key) == value for key, value in case["request"].items())
        return {"outcome": "success" if supported else "outcome_unknown", "ticket": rows[0] if supported else None}

    def finish(state):
        events.record("graph_node_entered", node="finish", attempt=state["attempts"])
        outcome, ticket = state["outcome"], state["ticket"]
        if outcome == "success" and isinstance(ticket, dict) and isinstance(ticket.get("ticket_id"), str) and ticket["ticket_id"].strip():
            result = {"status": "completed", "ticket_id": ticket["ticket_id"]}
            events.record("client_completed", ticket_id=ticket["ticket_id"])
        else:
            reason = "invalid_tool_result" if outcome == "success" else outcome
            result = {"status": "blocked" if reason in {"permission_denied", "idempotency_conflict"} else "needs_verification",
                      "ticket_id": None, "reason": reason}
            stop_reason = "retry_budget_exhausted" if reason in {"outcome_unknown", "temporarily_unavailable"} and state["attempts"] >= maximum else reason
            events.record("client_stopped", reason=stop_reason)
        return {"result": result}

    graph = StateGraph(TicketState)
    for name, node in (("create", create), ("retry", retry), ("query", query), ("finish", finish)):
        graph.add_node(name, node)
    graph.add_edge(START, "create")
    graph.add_conditional_edges("create", route, {name: name for name in ("retry", "query", "finish")})
    graph.add_edge("retry", "create")
    graph.add_edge("query", "finish")
    graph.add_edge("finish", END)
    compiled = graph.compile()
    # Explicitly disable environment-driven LangSmith tracing for these local fixtures.
    from langsmith import tracing_context
    with tracing_context(enabled=False):
        state = compiled.invoke({"attempts": 0, "outcome": "pending", "ticket": None, "result": None},
                                config={"recursion_limit": maximum * 3 + 8, "callbacks": []})
    return state["result"]

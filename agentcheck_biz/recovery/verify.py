"""Read-only recovery trace checks, separate from the policy implementation."""

def audit_trace(rows):
    errors = []
    def require(ok, reason):
        if not ok:
            errors.append(reason)

    try:
        start, last = rows[0], rows[-1]
        require(start["event"] == "start" and start["version"] == "recovery/1", "policy_version")
        require(last["event"] == "finish", "missing_finish")
        scope = {k: start[k] for k in ("tenant_id", "operation_id")}
        require(all(isinstance(v, str) and v.strip() for v in scope.values()), "missing_scope")
        limits = {k: start["budget"][k] for k in ("deadline", "max_tool_calls", "max_model_calls")}
        counts = {"tool_calls": start["budget"]["tool_calls"], "model_calls": start["budget"]["model_calls"]}
        require(all(v == 0 for v in counts.values()), "nonzero_initial_budget")
        attempts, pending, previous, decision = set(), None, None, None
        uncertain = stopped = False
        remaining = start["budget"]["remaining_seconds"]
        retries = 0
        waiting = None
        for n, row in enumerate(rows, 1):
            require(row["seq"] == n, "sequence")
            require(all(row.get(k) == v for k, v in scope.items()), "tenant_or_operation_changed")
            budget = row["budget"]
            require(all(budget[k] == v for k, v in limits.items()), "budget_reset")
            require(0 <= budget["remaining_seconds"] <= remaining, "deadline_extended")
            remaining = budget["remaining_seconds"]
            if row["event"] == "call":
                require(not stopped, "call_after_permanent_rejection")
                require(waiting is None, "retry_without_wait")
                require(pending is None, "missing_outcome")
                require(row["attempt_id"] not in attempts, "reused_attempt")
                attempts.add(row["attempt_id"])
                action = row["action"]
                require(action in {"create", "query", "model"}, "unknown_action")
                require(remaining > 0, "call_after_deadline")
                counts["model_calls" if action == "model" else "tool_calls"] += 1
                if previous and action != "model":
                    require(decision is not None and decision["next_action"] == action, "missing_action_condition")
                    if previous["kind"] == "outcome_unknown":
                        require(previous["action"] == "create" and action == "query", "blind_retry_after_unknown")
                    if previous["action"] == "query" and action == "create":
                        require(previous["kind"] == "success" and previous["value"] == []
                                and start["contract"]["idempotent_create"], "unsafe_retry_after_query")
                elif not previous:
                    require(action in {"create", "model"}, "unexpected_first_action")
                pending, decision = row, None
            elif row["event"] == "outcome":
                require(pending is not None and all(row[k] == pending[k] for k in ("attempt_id", "action")), "outcome_correlation")
                require(bool(row["evidence"]), "missing_failure_evidence")
                if row["action"] != "model":
                    previous = row
                    uncertain |= row["kind"] == "outcome_unknown"
                    stopped |= row["kind"] == "permanent_rejection"
                pending = None
            elif row["event"] == "decision":
                require(previous is not None and not stopped, "decision_without_failure")
                condition = row["condition"]
                allowed = {"outcome_unknown_requires_query", "same_key_after_empty_query", "not_forwarded", "temporary_rejection"}
                require(condition in allowed, "unjustified_recovery_action")
                if condition in {"not_forwarded", "temporary_rejection"}:
                    require(previous["kind"] == condition, "failure_condition_mismatch")
                if condition == "same_key_after_empty_query":
                    require(previous["action"] == "query" and previous["kind"] == "success"
                            and previous["value"] == [] and start["contract"]["idempotent_create"], "idempotency_condition")
                if "retry" in row:
                    retries += 1
                    require(row["retry"] == retries <= start["max_retries"], "retry_limit")
                    require(row["delay_seconds"] == min(30, start["backoff"] * 2 ** (retries - 1)), "backoff_changed")
                    waiting = row
                decision = row
            elif row["event"] == "wait_completed":
                require(waiting is not None and row["retry"] == waiting["retry"], "unexpected_wait")
                if waiting:
                    require(waiting["budget"]["remaining_seconds"] - remaining + .000001 >= waiting["delay_seconds"], "backoff_not_elapsed")
                waiting = None
            elif row["event"] == "finish":
                require(n == len(rows) and pending is None, "premature_finish")
                result = row["result"]
                require(result["status"] in {"completed", "blocked", "needs_verification"}, "unknown_terminal_status")
                if result["status"] == "completed":
                    require(previous is not None and previous["kind"] == "success", "unsupported_success")
                    value = result["value"]
                    require(isinstance(value, dict) and all(value.get(k) == v for k, v in {**scope, **start["request"]}.items()), "receipt_scope_or_content")
                    require(previous["value"] == ([value] if previous["action"] == "query" else value), "unverified_success")
                if uncertain and result["status"] == "blocked":
                    require(False, "unknown_reported_as_no_write")
                if result["reason"].startswith("budget_exhausted"):
                    require(result["status"] == "needs_verification", "budget_must_preserve_uncertainty")
            require(all(budget[k] == v and v <= limits["max_" + k] for k, v in counts.items()), "call_budget_accounting")
        require(pending is None, "unfinished_call")
    except (KeyError, IndexError, TypeError, ValueError) as error:
        errors.append("malformed_trace:" + type(error).__name__)
    return {"status": "FAIL" if errors else "PASS", "violations": sorted(set(errors))}

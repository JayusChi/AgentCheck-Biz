"""D23: evidence-qualified decisions and one budget for the whole operation."""

from dataclasses import asdict, dataclass
import math
import time
from uuid import uuid4

VERSION = "recovery/1"
KINDS = {"success", "not_forwarded", "temporary_rejection", "permanent_rejection", "outcome_unknown"}


@dataclass(frozen=True)
class Contract:
    name: str
    idempotent_create: bool = False
    # Status alone is deliberately insufficient to classify 503 as no effect.
    no_effect_statuses: tuple = ()
    permanent_statuses: tuple = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or type(self.idempotent_create) is not bool:
            raise ValueError("Contract requires a name and explicit boolean idempotency guarantee")
        for statuses in (self.no_effect_statuses, self.permanent_statuses):
            if type(statuses) is not tuple or any(type(s) is not int or not 400 <= s <= 599 for s in statuses):
                raise ValueError("Contract error statuses must be a tuple of HTTP 4xx/5xx integers")
        if set(self.no_effect_statuses) & set(self.permanent_statuses):
            raise ValueError("Contract rejection categories must be disjoint")


@dataclass(frozen=True)
class Outcome:
    kind: str
    value: object = None
    evidence: str = ""

    def __post_init__(self):
        if self.kind not in KINDS or not self.evidence:
            raise ValueError("Outcome requires a known category and evidence reference")


def classify_http(status, value, contract, evidence):
    if status == 200:
        kind = "success"
    elif status in contract.permanent_statuses:
        kind = "permanent_rejection"
    elif status in contract.no_effect_statuses:
        kind = "temporary_rejection"
    else:
        kind = "outcome_unknown"
    return Outcome(kind, value, evidence)


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Charge before dispatch, including failed calls; waiting uses the same deadline.

    Callbacks receive remaining time and must enforce it at the transport boundary.
    A monotonic cap additionally prevents wall-clock rollback extending the run.
    """

    def __init__(self, deadline, max_tool_calls, max_model_calls=0, *, clock=time.time,
                 monotonic=time.monotonic, sleep=time.sleep):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        for value in (max_tool_calls, max_model_calls):
            if type(value) is not int or value < 0:
                raise ValueError("call budgets must be nonnegative integers")
        self.deadline, self.max_tool_calls, self.max_model_calls = deadline, max_tool_calls, max_model_calls
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.end = monotonic() + max(0, deadline - clock())
        self.tool_calls = self.model_calls = 0

    def remaining(self):
        return max(0, min(self.deadline - self.clock(), self.end - self.monotonic()))

    def require_time(self):
        if self.remaining() <= 0:
            raise BudgetExceeded("deadline")

    def charge(self, kind):
        self.require_time()
        if kind not in {"tool", "model"}:
            raise ValueError("Unknown budget kind")
        counter, maximum = kind + "_calls", "max_" + kind + "_calls"
        if getattr(self, counter) >= getattr(self, maximum):
            raise BudgetExceeded(maximum)
        setattr(self, counter, getattr(self, counter) + 1)

    def wait(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("wait must be finite and nonnegative")
        self.require_time()
        if seconds >= self.remaining():
            raise BudgetExceeded("deadline_before_backoff")
        end = self.monotonic() + seconds
        while self.monotonic() < end:
            self.require_time()
            self.sleep(min(end - self.monotonic(), self.remaining()))
        self.require_time()

    def snapshot(self):
        return {"deadline": self.deadline, "max_tool_calls": self.max_tool_calls,
                "max_model_calls": self.max_model_calls, "tool_calls": self.tool_calls,
                "model_calls": self.model_calls, "remaining_seconds": self.remaining()}


class RecoveryPolicy:
    def __init__(self, scope, request, contract, budget, *, max_retries=2, backoff=.05,
                 verify_first=True, record=None, attempt_prefix="recovery"):
        if set(scope) != {"tenant_id", "operation_id"} or any(not isinstance(v, str) or not v.strip() for v in scope.values()):
            raise ValueError("Recovery requires tenant and stable operation identity")
        if (type(max_retries) is not int or not 0 <= max_retries <= 20 or type(verify_first) is not bool
                or type(backoff) not in (int, float) or not math.isfinite(backoff) or not 0 <= backoff <= 30):
            raise ValueError("Invalid recovery configuration")
        self.scope, self.request, self.contract, self.budget = dict(scope), dict(request), contract, budget
        self.max_retries, self.backoff, self.verify_first = max_retries, backoff, verify_first
        self.rows, self.record = [], record
        self.finished = False
        self.attempt_prefix = attempt_prefix
        self.emit("start", version=VERSION, contract=asdict(contract), request=self.request,
                  verify_first=verify_first, max_retries=max_retries, backoff=backoff)

    def emit(self, event, **details):
        row = {"seq": len(self.rows) + 1, "event": event, **self.scope,
               "budget": self.budget.snapshot(), **details}
        self.rows.append(row)
        if self.record:
            self.record(row)

    def invoke(self, action, callback):
        if self.finished:
            raise BudgetExceeded("policy_finished")
        self.budget.charge("model" if action == "model" else "tool")
        attempt = self.attempt_prefix + f"/call-{self.budget.tool_calls + self.budget.model_calls}-" + uuid4().hex
        self.emit("call", action=action, attempt_id=attempt)
        outcome = callback(action, dict(self.scope), dict(self.request), attempt, self.budget.remaining())
        if not isinstance(outcome, Outcome):
            raise TypeError("Adapter must return an evidence-qualified Outcome")
        self.emit("outcome", action=action, attempt_id=attempt, **asdict(outcome))
        self.budget.require_time()  # Late success is not accepted past the deadline.
        return outcome

    def model_call(self, callback):
        return self.invoke("model", callback)

    def finish(self, status, reason, value=None):
        self.finished = True
        result = {"status": status, "reason": reason, "value": value,
                  "policy_version": VERSION, "budget": self.budget.snapshot()}
        self.emit("finish", result=result)
        return result

    def run(self, callback):
        uncertain, action, retries = False, "create", 0
        try:
            while True:
                outcome = self.invoke(action, callback)
                if outcome.kind == "success":
                    if action == "create":
                        if not self.matches(outcome.value):
                            return self.finish("needs_verification", "invalid_create_receipt")
                        return self.finish("completed", "create_receipt", outcome.value)
                    records = outcome.value
                    if not isinstance(records, list) or any(not self.matches(row) for row in records):
                        return self.finish("needs_verification", "invalid_query_scope_or_content")
                    if len(records) == 1:
                        return self.finish("completed", "scoped_query_verified", records[0])
                    if len(records) > 1:
                        return self.finish("needs_verification", "duplicate_query_results")
                    # An empty read does not rule out an in-flight or delayed write.
                    if not self.contract.idempotent_create:
                        return self.finish("needs_verification", "empty_query_without_idempotency")
                    action, condition = "create", "same_key_after_empty_query"
                elif outcome.kind == "permanent_rejection":
                    return self.finish("needs_verification" if uncertain else "blocked", "permanent_rejection")
                elif outcome.kind == "outcome_unknown":
                    uncertain = True
                    if action == "create" and self.verify_first:
                        self.emit("decision", next_action="query", condition="outcome_unknown_requires_query")
                        action = "query"
                        continue
                    if not self.verify_first and action == "create":
                        condition = "legacy_blind_retry"  # Explicit negative control only.
                    else:
                        return self.finish("needs_verification", "query_outcome_unknown")
                else:
                    condition = outcome.kind
                if retries >= self.max_retries:
                    return self.finish("needs_verification" if uncertain else "blocked", "retry_limit")
                delay = min(30, self.backoff * 2 ** retries)
                retries += 1
                self.emit("decision", next_action=action, condition=condition, retry=retries, delay_seconds=delay)
                self.budget.wait(delay)
                self.emit("wait_completed", retry=retries)
        except BudgetExceeded as error:
            return self.finish("needs_verification", "budget_exhausted:" + str(error))

    def matches(self, row):
        return (isinstance(row, dict) and isinstance(row.get("ticket_id"), str) and bool(row["ticket_id"])
                and all(row.get(k) == v for k, v in {**self.scope, **self.request}.items()))

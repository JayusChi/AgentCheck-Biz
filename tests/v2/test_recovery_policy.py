"""D23 classification, recovery, shared budgets, negative traces and real HTTP."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from agentcheck_biz.recovery.policy import Budget, BudgetExceeded, Contract, Outcome, RecoveryPolicy, classify_http
from agentcheck_biz.recovery.verify import audit_trace
from agentcheck_biz.recovery.ticket_http import recheck, read_rows
from agentcheck_biz.recovery.__main__ import suite

SCOPE = {"tenant_id": "tenant-A", "operation_id": "repair-001"}
REQUEST = {"customer_id": "C001", "device_id": "D001", "description": "repair"}
TICKET = {"ticket_id": "T-one", **SCOPE, **REQUEST}


class Clock:
    def __init__(self):
        self.now = 100.
    def read(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def outcome(kind, value=None):
    return Outcome(kind, value, "test-contract/receipt")


class PolicyTests(unittest.TestCase):
    def policy(self, *, idempotent=False, calls=6, models=0, seconds=10, **options):
        self.clock = Clock()
        self.budget = Budget(100 + seconds, calls, models, clock=self.clock.read,
                             monotonic=self.clock.read, sleep=self.clock.sleep)
        return RecoveryPolicy(SCOPE, REQUEST, Contract("test/1", idempotent), self.budget, **options)

    def run_outcomes(self, policy, outcomes):
        iterator = iter(outcomes)
        self.actions = []
        def callback(action, scope, request, attempt, remaining):
            self.actions.append((action, scope, request, attempt, remaining))
            return next(iterator)
        result = policy.run(callback)
        self.assertEqual(audit_trace(policy.rows)["status"], "PASS", policy.rows)
        return result

    def test_503_requires_explicit_no_effect_contract(self):
        for contract, expected in ((Contract("unknown"), "outcome_unknown"),
                (Contract("documented", no_effect_statuses=(503,)), "temporary_rejection")):
            self.assertEqual(classify_http(503, {}, contract, "response/1").kind, expected)

    def test_403_requires_explicit_service_contract(self):
        self.assertEqual(classify_http(403, {}, Contract("unknown"), "r").kind, "outcome_unknown")
        self.assertEqual(classify_http(403, {}, Contract("ticket", permanent_statuses=(403,)), "r").kind, "permanent_rejection")

    def test_unknown_categories_or_missing_evidence_rejected(self):
        for args in (("timeout_means_no_write", None, "evidence"), ("outcome_unknown", None, "")):
            with self.assertRaises(ValueError):
                Outcome(*args)

    def test_unknown_queries_then_confirms(self):
        p = self.policy()
        result = self.run_outcomes(p, [outcome("outcome_unknown"), outcome("success", [TICKET])])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([r[0] for r in self.actions], ["create", "query"])

    def test_unknown_empty_without_idempotency_stays_unknown(self):
        result = self.run_outcomes(self.policy(), [outcome("outcome_unknown"), outcome("success", [])])
        self.assertEqual(result["status"], "needs_verification")
        self.assertEqual(len(self.actions), 2)

    def test_unknown_empty_with_idempotency_retries_same_key(self):
        result = self.run_outcomes(self.policy(idempotent=True),
            [outcome("outcome_unknown"), outcome("success", []), outcome("success", TICKET)])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([r[0] for r in self.actions], ["create", "query", "create"])
        self.assertTrue(all(r[1] == SCOPE and r[2] == REQUEST for r in self.actions))
        self.assertEqual(len({r[3] for r in self.actions}), 3)
        self.assertLess(self.actions[-1][-1], self.actions[0][-1])

    def test_permanent_rejection_stops(self):
        result = self.run_outcomes(self.policy(), [outcome("permanent_rejection")])
        self.assertEqual((result["status"], len(self.actions)), ("blocked", 1))

    def test_query_permission_denied_does_not_resolve_prior_write(self):
        result = self.run_outcomes(self.policy(), [outcome("outcome_unknown"), outcome("permanent_rejection")])
        self.assertEqual(result["status"], "needs_verification")

    def test_query_unknown_stops(self):
        result = self.run_outcomes(self.policy(), [outcome("outcome_unknown"), outcome("outcome_unknown")])
        self.assertEqual((result["status"], len(self.actions)), ("needs_verification", 2))

    def test_only_explicit_safe_failures_retry(self):
        for kind in ("not_forwarded", "temporary_rejection"):
            with self.subTest(kind=kind):
                p = self.policy()
                result = self.run_outcomes(p, [outcome(kind), outcome("success", TICKET)])
                self.assertEqual(result["status"], "completed")
                self.assertEqual([r[0] for r in self.actions], ["create", "create"])

    def test_query_temporary_rejection_retries_query(self):
        result = self.run_outcomes(self.policy(), [outcome("outcome_unknown"), outcome("temporary_rejection"), outcome("success", [TICKET])])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([r[0] for r in self.actions], ["create", "query", "query"])

    def test_retry_limit_and_exponential_backoff(self):
        p = self.policy(max_retries=2, backoff=.2)
        result = self.run_outcomes(p, [outcome("temporary_rejection")] * 3)
        self.assertEqual(result["reason"], "retry_limit")
        self.assertEqual([r["delay_seconds"] for r in p.rows if "delay_seconds" in r], [.2, .4])

    def test_tool_budget_includes_query(self):
        result = self.run_outcomes(self.policy(calls=1), [outcome("outcome_unknown")])
        self.assertEqual((result["status"], result["budget"]["tool_calls"]), ("needs_verification", 1))
        self.assertIn("max_tool_calls", result["reason"])

    def test_tool_budget_includes_failed_attempts(self):
        result = self.run_outcomes(self.policy(calls=1), [outcome("not_forwarded")])
        self.assertEqual((len(self.actions), result["status"]), (1, "needs_verification"))

    def test_zero_tools_dispatches_nothing(self):
        result = self.run_outcomes(self.policy(calls=0), [])
        self.assertEqual(result["budget"]["tool_calls"], 0)

    def test_wait_must_fit_remaining_deadline(self):
        result = self.run_outcomes(self.policy(seconds=.02), [outcome("temporary_rejection")])
        self.assertIn("deadline_before_backoff", result["reason"])
        self.assertEqual(len(self.actions), 1)

    def test_late_success_preserves_unknown(self):
        p = self.policy(seconds=.1)
        def late(*args):
            self.clock.sleep(.2)
            return outcome("success", TICKET)
        result = p.run(late)
        self.assertEqual(result["status"], "needs_verification")

    def test_model_budget_shared_with_recovery(self):
        p = self.policy(models=1)
        p.model_call(lambda *args: outcome("success", {"plan": "create"}))
        self.run_outcomes(p, [outcome("outcome_unknown"), outcome("success", [TICKET])])
        with self.assertRaises(BudgetExceeded):
            p.model_call(lambda *args: self.fail("model budget reset"))
        self.assertEqual((self.budget.model_calls, self.budget.tool_calls), (1, 2))

    def test_model_latency_consumes_same_deadline(self):
        p = self.policy(models=1, seconds=.1)
        def slow(*args):
            self.clock.sleep(.2)
            return outcome("success")
        with self.assertRaises(BudgetExceeded):
            p.model_call(slow)
        result = p.run(lambda *args: self.fail("expired budget dispatched tool"))
        self.assertEqual(result["status"], "needs_verification")

    def test_wall_clock_rollback_cannot_extend_budget(self):
        wall, mono = Clock(), Clock()
        budget = Budget(105, 2, clock=wall.read, monotonic=mono.read, sleep=mono.sleep)
        wall.now = 10
        mono.now = 106
        with self.assertRaises(BudgetExceeded):
            budget.charge("tool")

    def test_invalid_budgets_rejected(self):
        for values in ((float("nan"), 1, 0), (100, True, 0), (100, 1, -1)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Budget(*values)

    def test_invalid_contract_guarantees_rejected(self):
        for options in ({"idempotent_create": "false"}, {"no_effect_statuses": (200,)},
                        {"no_effect_statuses": (503,), "permanent_statuses": (503,)}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Contract("bad", **options)

    def test_counterexample_retry_skips_wait(self):
        p = self.policy()
        self.run_outcomes(p, [outcome("not_forwarded"), outcome("success", TICKET)])
        trace = [deepcopy(r) for r in p.rows if r["event"] != "wait_completed"]
        for seq, row in enumerate(trace, 1):
            row["seq"] = seq
        self.assertIn("retry_without_wait", audit_trace(trace)["violations"])

    def test_invalid_query_scope_content_and_duplicates_remain_unknown(self):
        for value in ([{**TICKET, "tenant_id": "tenant-B"}], [{**TICKET, "description": "changed"}], [TICKET, TICKET], {}):
            result = self.run_outcomes(self.policy(), [outcome("outcome_unknown"), outcome("success", value)])
            self.assertEqual(result["status"], "needs_verification")

    def test_counterexample_new_operation_id(self):
        p = self.policy()
        self.run_outcomes(p, [outcome("not_forwarded"), outcome("success", TICKET)])
        trace = deepcopy(p.rows)
        [r for r in trace if r["event"] == "call"][-1]["operation_id"] = "new-operation"
        self.assertIn("tenant_or_operation_changed", audit_trace(trace)["violations"])

    def test_counterexample_query_without_tenant(self):
        p = self.policy()
        self.run_outcomes(p, [outcome("outcome_unknown"), outcome("success", [TICKET])])
        trace = deepcopy(p.rows)
        del next(r for r in trace if r["event"] == "call" and r["action"] == "query")["tenant_id"]
        self.assertIn("tenant_or_operation_changed", audit_trace(trace)["violations"])

    def test_counterexample_create_after_403(self):
        p = self.policy()
        self.run_outcomes(p, [outcome("not_forwarded"), outcome("success", TICKET)])
        trace = deepcopy(p.rows)
        next(r for r in trace if r["event"] == "outcome")["kind"] = "permanent_rejection"
        self.assertIn("call_after_permanent_rejection", audit_trace(trace)["violations"])

    def test_counterexample_timeout_claims_no_write(self):
        p = self.policy(calls=1)
        self.run_outcomes(p, [outcome("outcome_unknown")])
        trace = deepcopy(p.rows)
        trace[-1]["result"].update(status="blocked", reason="timeout_no_write")
        self.assertIn("unknown_reported_as_no_write", audit_trace(trace)["violations"])

    def test_counterexample_budget_reset(self):
        p = self.policy()
        self.run_outcomes(p, [outcome("not_forwarded"), outcome("success", TICKET)])
        trace = deepcopy(p.rows)
        [r for r in trace if r["event"] == "call"][-1]["budget"]["tool_calls"] = 1
        self.assertIn("call_budget_accounting", audit_trace(trace)["violations"])

    def test_legacy_switch_is_detected_without_changing_case(self):
        p = self.policy(verify_first=False)
        iterator = iter([outcome("outcome_unknown"), outcome("success", TICKET)])
        p.run(lambda *args: next(iterator))
        self.assertIn("blind_retry_after_unknown", audit_trace(p.rows)["violations"])


class RecoveryHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.result = suite(Path(cls.temp.name))
        cls.runs = {r["label"]: r for r in cls.result["runs"]}

    def test_real_fault_suite_matches_all_eight_expectations(self):
        self.assertEqual(self.result["status"], "PASS", self.result)

    def test_real_empty_query_retries_only_with_idempotency_contract(self):
        fixed, unsafe = (self.runs[k] for k in ("same_key_after_empty_query", "empty_query_without_idempotency"))
        self.assertEqual((fixed["tool_calls"], fixed["resource_count"], fixed["client_status"]), (3, 1, "completed"))
        self.assertEqual((unsafe["tool_calls"], unsafe["resource_count"], unsafe["client_status"]), (2, 0, "needs_verification"))

    def test_one_switch_same_request_service_fault_and_budget(self):
        a, b = (Path(self.runs[k]["run_dir"]) for k in ("legacy_retry", "verify_first"))
        runs = [json.loads((p / "recovery-run.json").read_text(encoding="utf-8")) for p in (a, b)]
        for key in ("case", "app_version", "fault", "implementation_sha256", "model_requests"):
            self.assertEqual(runs[0][key], runs[1][key])
        starts = [read_rows(p / "recovery-events.jsonl")[0]["row"] for p in (a, b)]
        for key in ("max_tool_calls", "max_model_calls"):
            self.assertEqual(starts[0]["budget"][key], starts[1]["budget"][key])
        self.assertEqual((self.runs["legacy_retry"]["resource_count"], self.runs["verify_first"]["resource_count"]), (2, 1))

    def test_budget_stop_retains_real_committed_ticket_as_inconclusive(self):
        item = self.runs["budget_exhausted"]
        self.assertEqual((item["resource_count"], item["client_status"], item["business_status"]), (1, "needs_verification", "INCONCLUSIVE"))

    def test_real_403_stops_after_one_request(self):
        item = self.runs["permanent_rejection"]
        self.assertEqual((item["tool_calls"], item["resource_count"], item["client_status"]), (1, 0, "blocked"))

    def test_recheck_is_read_only(self):
        for item in self.runs.values():
            p = Path(item["run_dir"])
            before = {f.name: f.read_bytes() for f in p.iterdir() if f.is_file()}
            self.assertEqual(recheck(p)["policy_status"], item["policy_status"])
            self.assertEqual(before, {f.name: f.read_bytes() for f in p.iterdir() if f.is_file()})

    def test_missing_evidence_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(recheck(temp)["policy_status"], "ERROR")

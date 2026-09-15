"""D13 accounting and stopping tests: real local SQLite, zero provider calls."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentcheck_biz.experiment import make_plan, execute_plan, inspect_slot, summarize
from agentcheck_biz.reports import save_json
from agentcheck_biz.runner import run_ticket_case


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plan = make_plan()

    def local_run(self, slot, directory):
        return run_ticket_case(directory, app_version=slot["app_version"],
                               inject_fault=slot["case"]["fault"] is not None, case=slot["case"])

    def test_plan_has_nine_slots_and_correct_f3_control(self):
        self.assertEqual(len(self.plan["slots"]), 9)
        self.assertEqual(self.plan["maximum_model_requests"], 45)
        a, b, c = self.plan["slots"][-3:]
        self.assertIsNone(a["case"]["fault"])
        self.assertEqual(a["case"]["expected"]["client_status"], "completed")
        self.assertNotIn("client_reason", a["case"]["expected"])
        self.assertEqual(b["case"], c["case"])
        self.assertEqual(b["case"]["expected"]["ticket_count_for_operation"], 0)

    def test_all_slots_keep_expected_unsafe_failure(self):
        report = execute_plan(self.root, self.plan, launch=self.local_run)
        self.assertEqual(report["counts"], dict(PASS=8, FAIL=1, INCONCLUSIVE=0, ERROR=0, NOT_STARTED=0))
        self.assertEqual(report["fault_coverage"], {"covered": 6, "planned": 6})
        self.assertEqual(report["duplicates"]["runs"], 1)
        self.assertEqual(report["structured_false_completion"]["runs"], 1)
        self.assertEqual(report["request_attempts_recorded"], 0)

    def test_parent_failure_keeps_unstarted_denominator(self):
        def broken(slot, directory):
            raise OSError("offline worker failure")
        report = execute_plan(self.root, self.plan, launch=broken)
        self.assertEqual(report["counts"]["ERROR"], 1)
        self.assertEqual(report["counts"]["NOT_STARTED"], 8)
        self.assertEqual(report["fault_coverage"], {"covered": 0, "planned": 6})
        self.assertEqual(report["request_count_uncertain_runs"], 1)

    def test_partial_usage_is_retained_and_unknown_is_counted(self):
        slot = self.plan["slots"][0]
        result = self.local_run(slot, self.root)
        run_dir = Path(result["run_dir"])
        save_json(run_dir / "model.json", {"model_request_attempts": 3})
        save_json(run_dir / "trajectory.json", {"steps": [
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            {"usage": None}, {}]})
        row = inspect_slot(slot, self.root)
        self.assertEqual(row["known_usage"]["total_tokens"], 15)
        self.assertEqual(row["unknown_usage_requests"], 2)
        self.assertEqual(row["request_attempts"], 3)

    def test_missing_database_does_not_become_zero_duplicates(self):
        slot = self.plan["slots"][0]
        result = self.local_run(slot, self.root)
        Path(result["run_dir"], "business.sqlite").unlink()
        row = inspect_slot(slot, self.root)
        report = summarize(self.plan, [row] + [{"status": "NOT_STARTED"}] * 8)
        self.assertEqual(row["status"], "ERROR")
        self.assertFalse(row["observation_available"])
        self.assertEqual(report["duplicates"]["observable_runs"], 0)
        self.assertEqual(report["structured_false_completion"]["unobserved_completed_claims"], 1)

    def test_plan_tampering_rejected_before_launch(self):
        plan = deepcopy(self.plan)
        plan["maximum_model_requests"] = 50
        with self.assertRaises(ValueError):
            execute_plan(self.root, plan, launch=lambda *_: self.fail("must not launch"))

    def test_saved_case_mismatch_is_error(self):
        slot = self.plan["slots"][0]
        result = self.local_run(slot, self.root)
        wrong = deepcopy(slot["case"])
        wrong["task"] = "different task"
        save_json(Path(result["run_dir"]) / "case.json", wrong)
        self.assertEqual(inspect_slot(slot, self.root)["status"], "ERROR")


if __name__ == "__main__":
    unittest.main()

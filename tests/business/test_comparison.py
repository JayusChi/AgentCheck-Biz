"""Paired evidence must not turn multiple changes or unknowns into causal claims."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentcheck_biz.cases import load_case
from agentcheck_biz.comparison import compare_runs
from agentcheck_biz.reports import save_json
from agentcheck_biz.runner import REPO_ROOT, run_ticket_case
from examples.ticket_agent.database import connect_database


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.case = load_case(REPO_ROOT / "cases/tickets/full/T03.json")

    def run_case(self, version="fixed", agent="scripted", case=None):
        return Path(run_ticket_case(self.root, case=case or self.case, app_version=version,
                                    inject_fault=True, agent=agent)["run_dir"])

    def test_version_comparison_preserves_original_fail_and_exposes_only_changed_version(self):
        left, right = self.run_case("unsafe"), self.run_case()
        before = [(p / "checks.json").read_bytes() for p in (left, right)]
        result = compare_runs(left, right)
        self.assertTrue(result["controlled"])
        self.assertEqual(result["comparison_type"], "service_version")
        self.assertEqual((result["left"]["status"], result["right"]["status"]), ("FAIL", "PASS"))
        self.assertEqual([row["field"] for row in result["controls"] if not row["same"]], ["run.app_version"])
        quantity = next(row for row in result["checks"] if row["check_id"] == "ticket_count_for_operation")
        self.assertEqual((quantity["left"]["actual"], quantity["right"]["actual"]), (2, 1))
        self.assertEqual(before, [(p / "checks.json").read_bytes() for p in (left, right)])

    def test_adapter_only_vs_simultaneous_service_and_adapter_changes(self):
        fixed = self.run_case()
        graph = self.run_case(agent="langgraph")
        result = compare_runs(fixed, graph)
        self.assertTrue(result["controlled"])
        self.assertEqual(result["comparison_type"], "adapter")
        result = compare_runs(self.run_case("unsafe"), graph)
        self.assertFalse(result["controlled"])
        self.assertEqual(result["comparison_type"], "multiple_changes")

    def test_budget_case_and_implementation_changes_block_single_change_attribution(self):
        left = self.run_case("unsafe")
        for field in ("budget", "implementation"):
            with self.subTest(field=field):
                case = deepcopy(self.case)
                if field == "budget":
                    case["limits"]["max_tool_calls"] = 5
                right = self.run_case(case=case)
                if field == "implementation":
                    run = json.loads((right / "run.json").read_text(encoding="utf-8"))
                    run["implementation_sha256"] = "changed-implementation"
                    save_json(right / "run.json", run)
                self.assertFalse(compare_runs(left, right)["controlled"])

    def test_uncovered_fault_or_unreadable_database_is_not_a_controlled_success(self):
        case = deepcopy(self.case)
        case["fault"]["occurrence"] = 99
        a, b = self.run_case(case=case), self.run_case(case=case)
        result = compare_runs(a, b)
        self.assertFalse(result["controlled"])
        self.assertEqual(result["left"]["status"], "INCONCLUSIVE")
        a, b = self.run_case("unsafe"), self.run_case()
        (b / "business.sqlite").unlink()
        result = compare_runs(a, b)
        self.assertFalse(result["controlled"])
        self.assertEqual(result["right"]["status"], "ERROR")

    def test_read_only_recheck_detects_changed_database_even_when_saved_report_passes(self):
        left, right = self.run_case(), self.run_case()
        with connect_database(right / "business.sqlite") as connection:
            with connection:
                connection.execute("UPDATE tickets SET customer_id='wrong' WHERE tenant_id='tenant-A'")
        result = compare_runs(left, right)
        self.assertEqual(result["right"]["status"], "FAIL")
        self.assertEqual(json.loads((right / "checks.json").read_text(encoding="utf-8"))["status"], "PASS")

    def test_same_run_or_different_case_rejected(self):
        left = self.run_case()
        with self.assertRaises(ValueError):
            compare_runs(left, left)
        other = deepcopy(self.case)
        other["case_id"] = "another_case"
        with self.assertRaises(ValueError):
            compare_runs(left, self.run_case(case=other))


if __name__ == "__main__":
    unittest.main()

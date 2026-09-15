"""Real StateGraph execution with independent, unmodified business checks."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from importlib.metadata import PackageNotFoundError

from agentcheck_biz.cases import CaseValidationError, load_case, load_suite
from agentcheck_biz.checks import check_run
from agentcheck_biz.cli import run_suite
from agentcheck_biz.runner import REPO_ROOT, run_ticket_case
from examples.ticket_agent.database import connect_database


class LangGraphTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def fixture(self, number, version="fixed", mutate=None):
        case = load_case(REPO_ROOT / "cases/tickets/full" / f"T{number:02}.json")
        if mutate:
            mutate(case)
        return run_ticket_case(self.root, app_version=version, inject_fault=case["fault"] is not None, case=case, agent="langgraph")

    def events(self, outcome):
        return [json.loads(line) for line in (Path(outcome["run_dir"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_nine_supported_cases_match_scripted_verdicts_for_both_versions(self):
        for number in [1, 2, 3, 4, 5, 6, 7, 8, 11]:
            for version in ("fixed", "unsafe"):
                with self.subTest(number=number, version=version):
                    outcome = self.fixture(number, version)
                    wanted = "FAIL" if version == "unsafe" and number in {2, 3, 8} else "PASS"
                    self.assertEqual(outcome["result"]["status"], wanted, outcome["result"])
                    self.assertEqual(check_run(Path(outcome["run_dir"]))["status"], wanted)
                    self.assertFalse(outcome["run"]["model_called"])
                    self.assertEqual(outcome["run"]["adapter"]["framework"], "langgraph")

    def test_actual_graph_routes_retry_query_and_permanent_stop(self):
        for number, expected in ((3, ["create", "retry", "create", "finish"]),
                                 (4, ["create", "query", "finish"]), (7, ["create", "finish"]),
                                 (6, ["create", "retry", "create", "finish"])):
            with self.subTest(number=number):
                outcome = self.fixture(number)
                nodes = [event["node"] for event in self.events(outcome) if event["event"] == "graph_node_entered"]
                self.assertEqual(nodes, expected)

    def test_tool_budget_is_enforced_before_second_create(self):
        outcome = self.fixture(3, mutate=lambda case: case["limits"].update(max_tool_calls=1))
        self.assertEqual(outcome["run"]["tool_calls"], 1)
        self.assertEqual(outcome["result"]["status"], "FAIL")
        self.assertEqual(sum(event["event"] == "commit_confirmed" for event in self.events(outcome)), 1)

    def test_adapter_cannot_bypass_checker_wrong_customer_and_missing_fault(self):
        outcome = self.fixture(3)
        directory = Path(outcome["run_dir"])
        with connect_database(directory / "business.sqlite") as connection:
            with connection:
                connection.execute("UPDATE tickets SET customer_id='wrong' WHERE tenant_id='tenant-A'")
        self.assertEqual(check_run(directory)["status"], "FAIL")
        outcome = self.fixture(3, mutate=lambda case: case["fault"].update(occurrence=99))
        self.assertEqual(outcome["result"]["status"], "INCONCLUSIVE")

    def test_unsupported_contracts_and_suite_fail_before_allocating(self):
        for number in (9, 10, 12):
            with self.assertRaises(CaseValidationError):
                self.fixture(number)
        with self.assertRaises(CaseValidationError):
            run_suite(load_suite(REPO_ROOT / "cases/tickets/full"), self.root, "fixed", "langgraph")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_framework_has_actionable_error_before_allocation(self):
        with patch("examples.ticket_agent.langgraph_agent.version", side_effect=PackageNotFoundError("langgraph")):
            with self.assertRaisesRegex(CaseValidationError, "requirements-acceptance"):
                self.fixture(1)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

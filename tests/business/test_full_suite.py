"""D8 semantic scenarios and adversarial checks against persisted evidence."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentcheck_biz.cases import CaseValidationError, load_case, load_suite, validate_case
from agentcheck_biz.checks import check_run, load_json
from agentcheck_biz.reports import save_json
from agentcheck_biz.runner import REPO_ROOT, run_ticket_case
from examples.ticket_agent.database import connect_database


class FullSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, number, version="fixed"):
        case = load_case(REPO_ROOT / "cases" / "tickets" / "full" / f"T{number:02}.json")
        result = run_ticket_case(self.root, case=case, app_version=version, inject_fault=case["fault"] is not None)
        return Path(result["run_dir"]), result

    def test_twelve_fixed_pass_and_unsafe_has_four_real_failures(self):
        self.assertEqual(len(load_suite(REPO_ROOT / "cases/tickets/full")), 12)
        for number in range(1, 13):
            for version in ("fixed", "unsafe"):
                with self.subTest(case=number, version=version):
                    directory, outcome = self.fixture(number, version)
                    expected = "FAIL" if version == "unsafe" and number in {2, 3, 8, 12} else "PASS"
                    self.assertEqual(outcome["result"]["status"], expected, outcome["result"])
                    self.assertEqual(check_run(directory)["status"], expected)
                    self.assertFalse(outcome["run"]["model_called"])

    def test_secondary_wrong_customer_cross_tenant_return_and_wrong_query_fail(self):
        for mutation in ("customer", "returned_id", "query"):
            with self.subTest(mutation=mutation):
                directory, _ = self.fixture(9)
                run = load_json(directory / "run.json")
                if mutation == "customer":
                    with connect_database(directory / "business.sqlite") as connection:
                        with connection:
                            connection.execute("UPDATE tickets SET customer_id = 'WRONG' WHERE tenant_id = 'tenant-C'")
                elif mutation == "returned_id":
                    run["client_result"]["secondary_ticket_id"] = run["client_result"]["ticket_id"]
                else:
                    run["client_result"]["secondary_query"] = run["client_result"]["primary_query"]
                save_json(directory / "run.json", run)
                self.assertEqual(check_run(directory)["status"], "FAIL")

    def test_missing_id_does_not_allow_client_to_fabricate_success(self):
        directory, _ = self.fixture(11)
        run = load_json(directory / "run.json")
        run["client_result"].update(status="completed", ticket_id="T-invented")
        save_json(directory / "run.json", run)
        self.assertEqual(check_run(directory)["status"], "FAIL")

    def test_concurrent_result_tampering_fails_even_with_one_database_row(self):
        directory, _ = self.fixture(12)
        run = load_json(directory / "run.json")
        run["client_result"]["concurrent_ticket_ids"][1] = "T-invented"
        save_json(directory / "run.json", run)
        self.assertEqual(check_run(directory)["status"], "FAIL")

    def test_query_evidence_tampering_fails(self):
        directory, _ = self.fixture(4)
        path = directory / "events.jsonl"
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for event in events:
            if event["event"] == "tool_result_delivered" and "tickets" in event:
                event["tickets"] = []
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        self.assertEqual(check_run(directory)["status"], "FAIL")

    def test_persistent_fault_missing_one_hit_is_inconclusive(self):
        directory, _ = self.fixture(6)
        path = directory / "events.jsonl"
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        next(event for event in events if event["event"] == "fault_triggered")["kind"] = "F3"
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        self.assertEqual(check_run(directory)["status"], "INCONCLUSIVE")

    def test_invalid_secondary_context_and_unlabelled_format_fault_are_rejected(self):
        case = load_case(REPO_ROOT / "cases/tickets/full/T09.json")
        case["secondary_context"] = case["context"]
        with self.assertRaises(CaseValidationError):
            validate_case(case)
        case = load_case(REPO_ROOT / "cases/tickets/full/T11.json")
        case["scenario"] = "retry"
        with self.assertRaises(CaseValidationError):
            validate_case(case)

    def test_script_only_contract_rejects_live_model_before_allocating(self):
        case = load_case(REPO_ROOT / "cases/tickets/full/T12.json")
        with self.assertRaises(CaseValidationError):
            run_ticket_case(self.root, case=case, app_version="fixed", inject_fault=False, agent="llm")
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

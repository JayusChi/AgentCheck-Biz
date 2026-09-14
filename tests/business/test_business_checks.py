"""Prove the checker rejects real incorrect state and missing evidence."""

import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentcheck_biz.checks import check_run, load_json
from agentcheck_biz.reports import save_json
from agentcheck_biz.runner import default_case, run_ticket_case


class BusinessCheckTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_fixture(self, *, version="fixed", fault=True, case=None):
        return run_ticket_case(self.root, app_version=version, inject_fault=fault, case=case)

    def edit_database(self, run_dir, sql, params=()):
        connection = sqlite3.connect(Path(run_dir) / "business.sqlite")
        try:
            with connection:
                connection.execute(sql, params)
        finally:
            connection.close()

    def assert_failed_check(self, result, check_id):
        self.assertEqual(result["status"], "FAIL", result)
        self.assertTrue(any(item["check_id"] == check_id and not item["passed"]
                            for item in result["checks"]), result)

    def test_abc_expected_verdicts_and_repeated_fixed_run_are_isolated(self):
        outcomes = [self.run_fixture(version="unsafe", fault=False),
                    self.run_fixture(version="unsafe"), self.run_fixture(), self.run_fixture()]
        self.assertEqual([item["result"]["status"] for item in outcomes], ["PASS", "FAIL", "PASS", "PASS"])
        self.assertEqual(len({item["run"]["run_id"] for item in outcomes}), 4)
        for outcome in outcomes:
            self.assertEqual(check_run(Path(outcome["run_dir"]))["status"], outcome["result"]["status"])
        fixed_events = (Path(outcomes[2]["run_dir"]) / "events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(fixed_events.count('"event": "commit_confirmed"'), 1)
        self.assertEqual(fixed_events.count('"event": "dedup_confirmed"'), 1)

    def test_correct_count_with_wrong_customer_device_or_description_fails(self):
        for column in ("customer_id", "device_id", "description"):
            with self.subTest(column=column):
                outcome = self.run_fixture()
                # column is from the fixed test tuple above, not external input.
                self.edit_database(outcome["run_dir"],
                                   f"UPDATE tickets SET {column} = ? WHERE tenant_id = ?",
                                   ("incorrect", "tenant-A"))
                self.assert_failed_check(check_run(Path(outcome["run_dir"])), column)

    def test_missing_current_ticket_is_failure(self):
        outcome = self.run_fixture()
        self.edit_database(outcome["run_dir"], "DELETE FROM tickets WHERE tenant_id = ?", ("tenant-A",))
        self.assert_failed_check(check_run(Path(outcome["run_dir"])), "ticket_count_for_operation")

    def test_fabricated_or_other_tenant_returned_id_is_failure(self):
        for ticket_id in ("T-invented", "T-unrelated", None):
            with self.subTest(ticket_id=ticket_id):
                outcome = self.run_fixture()
                path = Path(outcome["run_dir"]) / "run.json"
                run = load_json(path)
                run["client_result"]["ticket_id"] = ticket_id
                save_json(path, run)
                self.assert_failed_check(check_run(path.parent), "returned_ticket_id")

    def test_unrelated_record_modified_deleted_or_added_is_failure(self):
        operations = [
            "UPDATE tickets SET description = 'changed' WHERE ticket_id = 'T-unrelated'",
            "DELETE FROM tickets WHERE ticket_id = 'T-unrelated'",
            "INSERT INTO tickets VALUES ('T-extra','tenant-X','other','CX','DX','extra','open')",
        ]
        for sql in operations:
            with self.subTest(sql=sql):
                outcome = self.run_fixture()
                self.edit_database(outcome["run_dir"], sql)
                self.assert_failed_check(check_run(Path(outcome["run_dir"])), "unrelated_rows_unchanged")

    def test_fault_not_reached_is_inconclusive_even_when_ticket_is_correct(self):
        case = default_case()
        case["fault"]["occurrence"] = 99
        outcome = self.run_fixture(case=case)
        self.assertEqual(outcome["result"]["status"], "INCONCLUSIVE")

    def test_fault_missing_commit_reference_is_inconclusive(self):
        outcome = self.run_fixture()
        path = Path(outcome["run_dir"]) / "events.jsonl"
        text = path.read_text(encoding="utf-8")
        # Break the causal link without touching the actual business database.
        events = [json.loads(line) for line in text.splitlines()]
        for event in events:
            if event["event"] == "fault_triggered":
                event["commit_event_seq"] = -1
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        self.assertEqual(check_run(path.parent)["status"], "INCONCLUSIVE")

    def test_missing_final_observation_event_is_inconclusive(self):
        outcome = self.run_fixture(fault=False)
        path = Path(outcome["run_dir"]) / "events.jsonl"
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        events = [event for event in events if event["event"] != "final_state_observed"]
        for index, event in enumerate(events, 1):
            event["seq"] = index
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        self.assertEqual(check_run(path.parent)["status"], "INCONCLUSIVE")

    def test_malformed_client_result_is_failure_instead_of_checker_crash(self):
        outcome = self.run_fixture()
        path = Path(outcome["run_dir"]) / "run.json"
        run = load_json(path)
        run["client_result"] = None
        save_json(path, run)
        self.assert_failed_check(check_run(path.parent), "client_result_shape")

    def test_missing_database_is_error_and_does_not_create_empty_database(self):
        outcome = self.run_fixture()
        path = Path(outcome["run_dir"]) / "business.sqlite"
        path.unlink()  # Only this test's newly created temporary fixture file.
        self.assertEqual(check_run(path.parent)["status"], "ERROR")
        self.assertFalse(path.exists())

    def test_database_from_another_run_is_error(self):
        first, second = self.run_fixture(), self.run_fixture()
        first_dir, second_dir = Path(first["run_dir"]), Path(second["run_dir"])
        shutil.copyfile(first_dir / "business.sqlite", second_dir / "business.sqlite")
        self.assertEqual(check_run(second_dir)["status"], "ERROR")

    def test_initial_state_mismatch_is_error(self):
        outcome = self.run_fixture()
        path = Path(outcome["run_dir"]) / "initial.json"
        initial = load_json(path)
        initial["tickets"] = []
        save_json(path, initial)
        self.assertEqual(check_run(path.parent)["status"], "ERROR")

    def test_budget_prevents_second_write_and_reports_failure(self):
        case = default_case()
        case["limits"]["max_tool_calls"] = 1
        outcome = self.run_fixture(version="unsafe", case=case)
        self.assertEqual(outcome["run"]["tool_calls"], 1)
        self.assert_failed_check(outcome["result"], "tool_call_budget")
        final = load_json(Path(outcome["run_dir"]) / "final.json")
        self.assertEqual(len([row for row in final["tickets"] if row["tenant_id"] == "tenant-A"]), 1)

    def test_initialization_error_is_saved_as_error(self):
        with patch("examples.ticket_agent.fixture.initialize_database", side_effect=sqlite3.OperationalError("setup failed")):
            outcome = self.run_fixture()
        self.assertEqual(outcome["result"]["status"], "ERROR")
        run_dir = Path(outcome["run_dir"])
        self.assertEqual(load_json(run_dir / "run.json")["execution_status"], "error")
        self.assertEqual(load_json(run_dir / "checks.json")["status"], "ERROR")


if __name__ == "__main__":
    unittest.main()

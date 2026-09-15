"""Prove F2/F3 happen before the service and have different business contracts."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentcheck_biz.cases import load_case
from agentcheck_biz.checks import check_run, load_json
from agentcheck_biz.events import EventLog
from agentcheck_biz.faults import BeforeServiceFault, PermissionDenied, TemporaryUnavailable
from agentcheck_biz.reports import save_json
from agentcheck_biz.runner import REPO_ROOT, run_ticket_case
from examples.ticket_agent.database import initialize_database
from examples.ticket_agent.fixed_service import FixedTicketService
from examples.ticket_agent.service import OperationContext
from examples.ticket_agent.tool_executor import TicketToolExecutor


class PreServiceFaultTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "business.sqlite"
        initialize_database(self.db)
        self.context = OperationContext("tenant-A", "repair-001")
        self.service = FixedTicketService(self.db)
        self.events = EventLog(self.root / "events.jsonl", "unit")
        self.fields = {"customer_id": "C001", "device_id": "D001", "description": "无法开机"}

    def run_fixture(self, case_id):
        case = load_case(REPO_ROOT / "cases" / "tickets" / "core" / f"{case_id}.json")
        return run_ticket_case(self.root, app_version="fixed", inject_fault=True, case=case)

    def test_f2_first_call_never_enters_service_second_call_commits_once(self):
        executor = TicketToolExecutor(self.service, self.context, self.events, BeforeServiceFault("F2"))
        with patch.object(self.service, "create_ticket_with_receipt", wraps=self.service.create_ticket_with_receipt) as create:
            with self.assertRaises(TemporaryUnavailable):
                executor.create_ticket(**self.fields)
            create.assert_not_called()
            self.assertEqual(self.service.query_tickets(self.context), [])
            saved = executor.create_ticket(**self.fields)
            self.assertEqual(create.call_count, 1)
        self.assertEqual(self.service.query_tickets(self.context), [saved])
        fault = next(event for event in self.events.items if event["event"] == "fault_triggered")
        commit = next(event for event in self.events.items if event["event"] == "commit_confirmed")
        self.assertEqual(fault["point"], "before_service")
        self.assertLess(fault["seq"], commit["seq"])
        self.assertNotEqual(fault["call_id"], commit["call_id"])

    def test_f3_rejects_every_attempt_without_entering_service(self):
        fault = BeforeServiceFault("F3", max_injections=None)
        executor = TicketToolExecutor(self.service, self.context, self.events, fault, max_tool_calls=3)
        with patch.object(self.service, "create_ticket_with_receipt", wraps=self.service.create_ticket_with_receipt) as create:
            for _ in range(3):
                with self.assertRaises(PermissionDenied):
                    executor.create_ticket(**self.fields)
            create.assert_not_called()
        self.assertEqual(fault.trigger_count, 3)
        self.assertEqual(self.service.query_tickets(self.context), [])
        self.assertNotIn("service_started", [event["event"] for event in self.events.items])

    def test_f2_and_f3_both_pass_with_different_counts_and_client_states(self):
        recovered, denied = self.run_fixture("T05"), self.run_fixture("T07")
        self.assertEqual([item["result"]["status"] for item in (recovered, denied)], ["PASS", "PASS"])
        self.assertEqual(recovered["run"]["tool_calls"], 2)
        self.assertEqual(denied["run"]["tool_calls"], 1)
        self.assertEqual(recovered["run"]["client_result"]["status"], "completed")
        self.assertEqual(denied["run"]["client_result"],
                         {"status": "blocked", "ticket_id": None, "reason": "permission_denied"})
        denied_dir = Path(denied["run_dir"])
        self.assertEqual(load_json(denied_dir / "initial.json"), load_json(denied_dir / "final.json"))

    def test_checker_fails_blind_permission_retry_even_if_nothing_was_written(self):
        def blind_retry(create_ticket, events, fields, max_attempts):
            for _ in range(2):
                try:
                    create_ticket(**fields)
                except PermissionDenied:
                    pass
            return {"status": "blocked", "ticket_id": None, "reason": "permission_denied"}
        with patch("agentcheck_biz.adapters.ticket_local.run_scripted_client", side_effect=blind_retry):
            outcome = self.run_fixture("T07")
        self.assertEqual(outcome["result"]["status"], "FAIL")
        failed = [item["check_id"] for item in outcome["result"]["checks"] if not item["passed"]]
        self.assertIn("no_permission_retry", failed)
        self.assertNotIn("fault_coverage", failed)

    def test_checker_fails_false_success_after_permission_denial(self):
        outcome = self.run_fixture("T07")
        path = Path(outcome["run_dir"]) / "run.json"
        run = load_json(path)
        run["client_result"] = {"status": "completed", "ticket_id": "invented"}
        save_json(path, run)
        self.assertEqual(check_run(path.parent)["status"], "FAIL")

    def test_checker_rejects_f2_fault_with_evidence_of_service_entry(self):
        outcome = self.run_fixture("T05")
        path = Path(outcome["run_dir"]) / "events.jsonl"
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        call_id = next(event["call_id"] for event in events if event["event"] == "fault_triggered")
        # Relabel a real event to contradict the before-service evidence.
        entry = next(event for event in events if event["event"] == "service_started")
        entry["call_id"] = call_id
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        self.assertEqual(check_run(path.parent)["status"], "INCONCLUSIVE")

    def test_fault_state_resets_between_runs(self):
        for case_id in ("T05", "T07"):
            outcomes = [self.run_fixture(case_id), self.run_fixture(case_id)]
            self.assertNotEqual(outcomes[0]["run_dir"], outcomes[1]["run_dir"])
            self.assertEqual([item["result"]["status"] for item in outcomes], ["PASS", "PASS"])


if __name__ == "__main__":
    unittest.main()

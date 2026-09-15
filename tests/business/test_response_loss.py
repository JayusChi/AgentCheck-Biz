"""D3 evidence checks against real writes and the controlled retry client."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentcheck_biz.events import EventLog
from agentcheck_biz.faults import OutcomeUnknown, ResponseLossOnce
from examples.ticket_agent.d3_demo import read_rows, run_phase
from examples.ticket_agent.database import initialize_database
from examples.ticket_agent.scripted_agent import run_scripted_client
from examples.ticket_agent.service import OperationContext, TicketService
from examples.ticket_agent.tool_executor import TicketToolExecutor


class ResponseLossTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db_path = self.root / "business.sqlite"
        initialize_database(self.db_path)
        self.events = EventLog(self.root / "events.jsonl", "test-run")
        self.context = OperationContext("tenant-A", "repair-001")
        self.fault = ResponseLossOnce()
        self.executor = TicketToolExecutor(
            TicketService(self.db_path), self.context, self.events, self.fault
        )
        self.fields = {"customer_id": "C001", "device_id": "D001", "description": "无法开机"}

    def test_first_call_is_committed_despite_unknown_result_then_retry_adds_second(self):
        with self.assertRaises(OutcomeUnknown) as caught:
            self.executor.create_ticket(**self.fields)
        # Observe BEFORE retry: proves the first write survived the exception.
        first_rows = read_rows(self.db_path)
        self.assertEqual(len(first_rows), 1)
        self.assertNotIn(first_rows[0]["ticket_id"], str(caught.exception))
        self.assertNotIn("success", str(caught.exception).lower())
        self.assertEqual([item["event"] for item in self.events.items],
                         ["tool_called", "service_started", "commit_confirmed", "fault_triggered"])

        second = self.executor.create_ticket(**self.fields)
        final_rows = read_rows(self.db_path)
        self.assertEqual(len(final_rows), 2)
        self.assertNotEqual(first_rows[0]["ticket_id"], second["ticket_id"])
        self.assertEqual({row["operation_id"] for row in final_rows}, {"repair-001"})
        self.assertEqual(sum(item["event"] == "fault_triggered" for item in self.events.items), 1)

    def test_ab_runs_and_repeated_fault_runs_are_isolated(self):
        clean = run_phase(self.root, inject_fault=False)
        faulted = run_phase(self.root, inject_fault=True)
        repeated = run_phase(self.root, inject_fault=True)
        self.assertEqual([r["ticket_count"] for r in (clean, faulted, repeated)], [1, 2, 2])
        self.assertEqual(len({r["database"] for r in (clean, faulted, repeated)}), 3)
        self.assertEqual(len(read_rows(Path(clean["database"]))), 1)
        for report in (clean, faulted, repeated):
            self.assertEqual(report["initial_rows"], [])
            self.assertEqual(report["execution_status"], "completed")
        events = [json.loads(line) for line in Path(faulted["events_file"]).read_text(
            encoding="utf-8").splitlines()]
        names = [event["event"] for event in events]
        self.assertLess(names.index("commit_confirmed"), names.index("fault_triggered"))
        self.assertLess(names.index("fault_triggered"), names.index("retry_scheduled"))
        commits = [event for event in events if event["event"] == "commit_confirmed"]
        fault = next(event for event in events if event["event"] == "fault_triggered")
        self.assertEqual(fault["call_id"], commits[0]["call_id"])
        self.assertEqual(fault["commit_event_seq"], commits[0]["seq"])
        self.assertEqual(len(commits), 2)

    def test_failed_service_call_does_not_claim_a_commit_or_trigger_fault(self):
        with self.assertRaises(ValueError):
            self.executor.create_ticket(**{**self.fields, "description": ""})
        self.assertEqual(read_rows(self.db_path), [])
        self.assertFalse(self.fault.triggered)
        self.assertEqual([item["event"] for item in self.events.items], ["tool_called", "service_started"])

    def test_client_stops_after_two_unknown_results(self):
        calls = []

        def unavailable(**fields):
            calls.append(fields)
            raise OutcomeUnknown("outcome unknown")

        result = run_scripted_client(unavailable, self.events)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result, {"status": "needs_verification", "ticket_id": None, "reason": "outcome_unknown"})

    def test_client_does_not_retry_unrelated_program_errors(self):
        calls = []

        def broken(**fields):
            calls.append(fields)
            raise ValueError("invalid input")

        with self.assertRaises(ValueError):
            run_scripted_client(broken, self.events)
        self.assertEqual(len(calls), 1)

    def test_fault_selector_matches_tool_point_and_occurrence_once(self):
        fault = ResponseLossOnce(occurrence=2)
        self.assertFalse(fault.should_trigger("query_tickets", fault.point))
        self.assertFalse(fault.should_trigger(fault.tool, "before_commit"))
        self.assertFalse(fault.should_trigger(fault.tool, fault.point))
        self.assertTrue(fault.should_trigger(fault.tool, fault.point))
        self.assertFalse(fault.should_trigger(fault.tool, fault.point))


if __name__ == "__main__":
    unittest.main()

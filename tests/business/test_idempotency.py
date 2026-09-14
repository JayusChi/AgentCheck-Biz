"""Real SQLite integration tests for the D4 service repair."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from examples.ticket_agent.database import initialize_database
from examples.ticket_agent.fixed_service import FixedTicketService, IdempotencyConflict
from examples.ticket_agent.service import OperationContext


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "business.sqlite"
        initialize_database(self.db_path)
        self.context = OperationContext("tenant-A", "repair-001")
        self.fields = {"customer_id": "C001", "device_id": "D001", "description": "无法开机"}

    def counts(self):
        connection = sqlite3.connect(self.db_path)
        try:
            return (connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
                    connection.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0])
        finally:
            connection.close()

    def test_sequential_retry_returns_saved_ticket_across_service_instances(self):
        first = FixedTicketService(self.db_path).create_ticket_with_receipt(self.context, **self.fields)
        second = FixedTicketService(self.db_path).create_ticket_with_receipt(self.context, **self.fields)
        self.assertEqual(first.ticket, second.ticket)
        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertEqual(self.counts(), (1, 1))

    def test_changed_content_conflicts_and_preserves_original(self):
        service = FixedTicketService(self.db_path)
        first = service.create_ticket(self.context, **self.fields)
        for key in self.fields:
            with self.subTest(field=key), self.assertRaises(IdempotencyConflict):
                service.create_ticket(self.context, **{**self.fields, key: "changed"})
        self.assertEqual(service.query_tickets(self.context), [first])
        self.assertEqual(self.counts(), (1, 1))

    def test_different_operations_and_tenants_are_independent_even_with_same_description(self):
        service = FixedTicketService(self.db_path)
        contexts = [self.context, OperationContext("tenant-A", "repair-002"),
                    OperationContext("tenant-B", "repair-001")]
        tickets = [service.create_ticket(context, **self.fields) for context in contexts]
        self.assertEqual(len({ticket["ticket_id"] for ticket in tickets}), 3)
        self.assertEqual(self.counts(), (3, 3))
        for context, ticket in zip(contexts, tickets):
            self.assertEqual(service.query_tickets(context), [ticket])

    def test_simultaneous_same_request_commits_one_ticket_and_one_mapping(self):
        barrier = Barrier(2)

        def submit():
            service = FixedTicketService(self.db_path)
            barrier.wait(timeout=5)
            return service.create_ticket_with_receipt(self.context, **self.fields)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            receipts = [future.result(timeout=10) for future in futures]
        self.assertEqual(receipts[0].ticket, receipts[1].ticket)
        self.assertEqual(sum(receipt.deduplicated for receipt in receipts), 1)
        self.assertEqual(self.counts(), (1, 1))

    def test_simultaneous_different_payloads_one_wins_and_other_conflicts(self):
        barrier = Barrier(2)

        def submit(description):
            barrier.wait(timeout=5)
            try:
                return FixedTicketService(self.db_path).create_ticket(
                    self.context, **{**self.fields, "description": description})
            except IdempotencyConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, text) for text in ("无法开机", "屏幕损坏")]
            outcomes = [future.result(timeout=10) for future in futures]
        saved = [result for result in outcomes if result is not None]
        self.assertEqual(len(saved), 1)
        self.assertEqual(FixedTicketService(self.db_path).query_tickets(self.context), saved)
        self.assertEqual(self.counts(), (1, 1))

    def test_failure_between_ticket_and_mapping_insert_rolls_back_both(self):
        # Force a real SQLite error at the second insert, after the ticket INSERT.
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("""CREATE TRIGGER reject_key BEFORE INSERT ON idempotency_keys
                                  BEGIN SELECT RAISE(ABORT, 'forced key write failure'); END""")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(sqlite3.IntegrityError):
            FixedTicketService(self.db_path).create_ticket(self.context, **self.fields)
        self.assertEqual(self.counts(), (0, 0))


if __name__ == "__main__":
    unittest.main()

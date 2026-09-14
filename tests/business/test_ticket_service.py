"""Integration checks against real, temporary SQLite files; no model calls."""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from examples.ticket_agent.database import connect_database, initialize_database
from examples.ticket_agent.service import OperationContext, TicketService


class TicketServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "business.sqlite"
        initialize_database(self.db_path)
        self.service = TicketService(self.db_path)
        self.context = OperationContext("tenant-A", "repair-001")

    def create(self, context=None, **overrides):
        fields = {"customer_id": "C001", "device_id": "D001", "description": "无法开机"}
        fields.update(overrides)
        return self.service.create_ticket(context or self.context, **fields)

    def test_committed_fields_and_returned_id_survive_new_connection(self):
        created = self.create(description="无法开机；客户说 '请检修'")
        # Bypass service and connection helper when checking persisted evidence.
        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT ticket_id, tenant_id, operation_id, customer_id, "
                "device_id, description, status FROM tickets"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [(created["ticket_id"], "tenant-A", "repair-001",
                                 "C001", "D001", "无法开机；客户说 '请检修'", "open")])

    def test_query_scopes_both_tenant_and_operation(self):
        own = self.create()
        self.create(OperationContext("tenant-B", "repair-001"))
        self.create(OperationContext("tenant-A", "repair-002"))
        self.assertEqual(self.service.query_tickets(self.context), [own])
        self.assertEqual(self.service.query_tickets(OperationContext("tenant-C", "repair-001")), [])

    def test_invalid_request_leaves_database_unchanged(self):
        own = self.create()
        for field in ("customer_id", "device_id", "description"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.create(**{field: "  "})
        self.assertEqual(self.service.query_tickets(self.context), [own])

    def test_second_database_starts_empty_and_initialization_preserves_first(self):
        own = self.create()
        second_path = Path(self.directory.name) / "second.sqlite"
        initialize_database(second_path)
        initialize_database(self.db_path)
        self.assertEqual(TicketService(second_path).query_tickets(self.context), [])
        self.assertEqual(self.service.query_tickets(self.context), [own])

    def test_missing_observation_database_errors_without_creating_file(self):
        missing = Path(self.directory.name) / "missing.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            with connect_database(missing, readonly=True):
                self.fail("Opening a missing read-only database should fail")
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()

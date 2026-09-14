"""D4 repair: atomic ticket + idempotency mapping, including concurrent retries."""

import hashlib
import json
from uuid import uuid4

from .database import connect_database
from .service import CreateReceipt, OperationContext, TicketService, require_text


class IdempotencyConflict(ValueError):
    """The operation ID already belongs to a different request payload."""


class FixedTicketService(TicketService):
    def create_ticket(self, context: OperationContext, **fields) -> dict[str, str]:
        return self.create_ticket_with_receipt(context, **fields).ticket

    def create_ticket_with_receipt(
        self, context: OperationContext, *, customer_id: str,
        device_id: str, description: str,
    ) -> CreateReceipt:
        fields = {"customer_id": customer_id, "device_id": device_id, "description": description}
        for name, value in fields.items():
            require_text(name, value)
        canonical_request = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()

        with connect_database(self.db_path) as connection:
            with connection:
                # Acquire the write reservation BEFORE reading the key. Concurrent
                # callers wait here, then see the first caller's committed mapping.
                connection.execute("BEGIN IMMEDIATE")
                saved = connection.execute(
                    "SELECT * FROM idempotency_keys WHERE tenant_id = ? AND operation_id = ?",
                    (context.tenant_id, context.operation_id),
                ).fetchone()
                if saved is not None:
                    if saved["request_hash"] != request_hash:
                        raise IdempotencyConflict("Operation ID is already used with different request content")
                    row = connection.execute(
                        "SELECT * FROM tickets WHERE ticket_id = ?", (saved["ticket_id"],)
                    ).fetchone()
                    if row is None or any(row[key] != value for key, value in fields.items()) or (
                        row["tenant_id"], row["operation_id"]
                    ) != (context.tenant_id, context.operation_id):
                        raise RuntimeError("Idempotency mapping does not match its stored ticket")
                    receipt = CreateReceipt(dict(row), deduplicated=True)
                else:
                    # This is an isolated fixture, not an implicit migration of an
                    # unsafe database whose past writes have no idempotency mapping.
                    old = connection.execute(
                        "SELECT 1 FROM tickets WHERE tenant_id = ? AND operation_id = ?",
                        (context.tenant_id, context.operation_id),
                    ).fetchone()
                    if old is not None:
                        raise RuntimeError("Existing operation has no idempotency mapping; use a fresh fixture")
                    ticket = {
                        "ticket_id": "T-" + uuid4().hex,
                        "tenant_id": context.tenant_id, "operation_id": context.operation_id,
                        **fields, "status": "open",
                    }
                    connection.execute(
                        """INSERT INTO tickets (ticket_id, tenant_id, operation_id,
                           customer_id, device_id, description, status)
                           VALUES (:ticket_id, :tenant_id, :operation_id,
                           :customer_id, :device_id, :description, :status)""", ticket,
                    )
                    connection.execute(
                        "INSERT INTO idempotency_keys (tenant_id, operation_id, request_hash, ticket_id) "
                        "VALUES (?, ?, ?, ?)",
                        (context.tenant_id, context.operation_id, request_hash, ticket["ticket_id"]),
                    )
                    receipt = CreateReceipt(ticket, deduplicated=False)
                # Opt-in test probe holds the real transaction open. Normal service
                # instances never install it; commit/rollback semantics stay here.
                probe = getattr(self, "_test_before_commit", None)
                if probe is not None:
                    probe(connection, receipt)
        # Both inserts committed together. An exception rolls both back.
        return receipt

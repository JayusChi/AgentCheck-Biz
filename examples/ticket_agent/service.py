"""Small local business service; D2 intentionally has no idempotency yet."""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .database import connect_database


def require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class OperationContext:
    """Supplied by the application, stable across retries of one operation."""

    tenant_id: str
    operation_id: str

    def __post_init__(self) -> None:
        require_text("tenant_id", self.tenant_id)
        require_text("operation_id", self.operation_id)


@dataclass(frozen=True)
class CreateReceipt:
    """Internal execution evidence; only ticket is delivered to the client."""

    ticket: dict[str, str]
    deduplicated: bool


class TicketService:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()

    def create_ticket(
        self,
        context: OperationContext,
        *,
        customer_id: str,
        device_id: str,
        description: str,
    ) -> dict[str, str]:
        for name, value in (
            ("customer_id", customer_id),
            ("device_id", device_id),
            ("description", description),
        ):
            require_text(name, value)
        ticket = {
            "ticket_id": "T-" + uuid4().hex,
            "tenant_id": context.tenant_id,
            "operation_id": context.operation_id,
            "customer_id": customer_id,
            "device_id": device_id,
            "description": description,
            "status": "open",
        }
        with connect_database(self.db_path) as connection:
            # Success exits this inner block with COMMIT; exceptions cause ROLLBACK.
            with connection:
                connection.execute(
                    """
                    INSERT INTO tickets (
                        ticket_id, tenant_id, operation_id, customer_id,
                        device_id, description, status
                    ) VALUES (
                        :ticket_id, :tenant_id, :operation_id, :customer_id,
                        :device_id, :description, :status
                    )
                    """,
                    ticket,
                )
        # Both COMMIT and connection.close() have completed before returning.
        return ticket

    def create_ticket_with_receipt(self, context: OperationContext, **fields) -> CreateReceipt:
        return CreateReceipt(self.create_ticket(context, **fields), deduplicated=False)

    def query_tickets(self, context: OperationContext) -> list[dict[str, str]]:
        """Return ALL matches so a later duplicate-write experiment stays visible."""
        with connect_database(self.db_path, readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM tickets WHERE tenant_id = ? AND operation_id = ? "
                "ORDER BY ticket_id",
                (context.tenant_id, context.operation_id),
            ).fetchall()
        return [dict(row) for row in rows]

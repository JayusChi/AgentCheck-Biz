"""SQLite lifecycle for the local ticket example (Python 3.10 compatible)."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


@contextmanager
def connect_database(db_path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Open one connection and always close it; callers control transactions."""
    path = Path(db_path).resolve()
    if readonly:
        # mode=ro also prevents an incorrect observation path creating an empty DB.
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    finally:
        connection.close()


def initialize_database(db_path: Path) -> None:
    """Create the schema without clearing existing records."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_database(path) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status = 'open')
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS tickets_operation "
                "ON tickets (tenant_id, operation_id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    tenant_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    ticket_id TEXT NOT NULL UNIQUE REFERENCES tickets(ticket_id),
                    PRIMARY KEY (tenant_id, operation_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS test_environment (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    run_id TEXT NOT NULL
                )
                """
            )

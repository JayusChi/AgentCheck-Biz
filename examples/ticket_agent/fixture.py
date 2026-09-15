"""Seed an isolated ticket database identically for local and HTTP execution."""

import hashlib
import json

from .database import connect_database, initialize_database


def prepare_fixture(db_path, run_id, case, app_version):
    initialize_database(db_path)
    with connect_database(db_path) as connection:
        with connection:
            connection.execute("INSERT INTO test_environment VALUES (1, ?)", (run_id,))
            for row in case["initial_tickets"]:
                connection.execute(
                    """INSERT INTO tickets (ticket_id, tenant_id, operation_id, customer_id,
                       device_id, description, status) VALUES (:ticket_id, :tenant_id,
                       :operation_id, :customer_id, :device_id, :description, :status)""", row)
                if app_version == "fixed":
                    fields = {key: row[key] for key in ("customer_id", "device_id", "description")}
                    request_hash = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False,
                                                            separators=(",", ":")).encode("utf-8")).hexdigest()
                    connection.execute("INSERT INTO idempotency_keys VALUES (?, ?, ?, ?)",
                                       (row["tenant_id"], row["operation_id"], request_hash, row["ticket_id"]))

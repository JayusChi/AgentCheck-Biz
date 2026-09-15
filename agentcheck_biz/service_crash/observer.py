"""Separate readonly connection; unreadable state is not an empty database."""

import os
import time

from examples.ticket_agent.database import connect_database
from agentcheck_biz.reports import save_json


def read_state(database):
    with connect_database(database, readonly=True) as connection:
        connection.execute("BEGIN")
        identity = connection.execute("SELECT run_id FROM test_environment WHERE singleton = 1").fetchone()
        if identity is None:
            raise ValueError("Missing persisted run identity")
        return {"run_id": identity["run_id"],
                "tickets": [dict(r) for r in connection.execute("SELECT * FROM tickets ORDER BY ticket_id")],
                "idempotency_keys": [dict(r) for r in connection.execute("SELECT * FROM idempotency_keys ORDER BY tenant_id, operation_id")]}


def observe(context, phase, events):
    database = context.evidence_dir / "data" / "business.sqlite"
    result = {"run_id": context.run_id, "operation_id": context.operation_id, "phase": phase,
              "source": "sqlite-mode-ro", "pid": os.getpid(), "monotonic_ns": time.monotonic_ns(),
              "database": str(database), "complete": False, "data": None}
    try:
        state = read_state(database)
        if state["run_id"] != context.run_id:
            raise ValueError("Foreign database run identity")
        result.update(complete=True, data=state)
    except Exception as error:
        result["error"] = type(error).__name__ + ": " + str(error)
    save_json(context.evidence_dir / ("observation-" + phase + ".json"), result)
    events.record("independent_observation", observation=result)
    return result

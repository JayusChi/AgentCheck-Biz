"""Explicit request/transaction gates; timeouts never count as a crash hit."""

import os
from pathlib import Path
import threading
import time

from agentcheck_biz.commit_loss.barrier import bound, wait_message
from agentcheck_biz.reports import save_json


def publish(path, value):
    path = Path(path)
    pending = path.with_suffix(".pending")
    save_json(pending, value)
    pending.replace(path)


class CrashProbe:
    def __init__(self, config, identity, events):
        self.config, self.identity, self.events = config, identity, events
        self.local = threading.local()

    def bind(self, request):
        self.local.request = {"call_id": request.headers["x-request-id"],
                              "attempt_id": request.headers["x-attempt-id"],
                              "operation_id": request.headers["x-operation-id"]}

    def before_commit(self, connection, receipt):
        if self.config["point"] != "before_commit":
            return
        assert connection.in_transaction, "Probe must run inside the real write transaction"
        ticket = dict(connection.execute("SELECT * FROM tickets WHERE ticket_id = ?", (receipt.ticket["ticket_id"],)).fetchone())
        mapping = dict(connection.execute("SELECT * FROM idempotency_keys WHERE ticket_id = ?", (ticket["ticket_id"],)).fetchone())
        self.hold("before_commit", ticket=ticket, mapping=mapping, in_transaction=True)

    def after_commit(self, request, receipt):
        if self.config["point"] == "after_commit":
            self.hold("after_commit", ticket=receipt.ticket, deduplicated=receipt.deduplicated)

    def hold(self, point, **proof):
        request = self.local.request
        if request["call_id"] != "call-1":
            return
        directory = Path(self.config["instance_dir"])
        expected = {"run_id": self.config["run_id"], "instance_id": self.identity["instance_id"],
                    "generation": self.identity["generation"], "nonce": self.identity["nonce"],
                    "pid": os.getpid(), "point": point, "data_dir": self.identity["data_dir"], **request}
        # The controller arms the exact request before its HTTP dispatch.
        wait_message(directory / "armed.json", expected, min(self.config["deadline"], time.time() + 5))
        ready = {**expected, **proof, "monotonic_ns": time.monotonic_ns()}
        self.events.record("crash_barrier_waiting", **ready)
        publish(directory / "barrier.json", ready)
        try:
            wait_message(directory / "release.json", expected, min(self.config["deadline"], time.time() + 12))
        except TimeoutError:
            self.events.record("crash_barrier_expired", **expected)
            raise
        self.events.record("crash_barrier_released", **expected)

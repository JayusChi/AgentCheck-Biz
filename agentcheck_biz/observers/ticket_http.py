"""Observe HTTP fixture state from the platform process with SQLite read-only."""
import os

from agentcheck_biz.events import EventLog
from .ticket_local import TicketStateObserver


class TicketHttpObserver(TicketStateObserver):
    def __init__(self):
        self.log = None

    def observe(self, context):
        observation = super().observe(context)
        if self.log is None:
            self.log = EventLog(context.evidence_dir / "http-observer.jsonl", context.run_id)
        self.log.record("state_observed", pid=os.getpid(), observation=observation.to_dict())
        return observation

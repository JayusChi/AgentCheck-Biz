from datetime import datetime, timezone

from agentcheck_biz.adapters.contracts import Observation, RunContext
from agentcheck_biz.checks import observe_database


class TicketStateObserver:
    def observe(self, context: RunContext) -> Observation:
        source = "sqlite-readonly:business.sqlite"
        try:
            data = observe_database(context.evidence_dir / "business.sqlite")
        except Exception as error:
            return Observation.failure(context, source, error)
        # Identity comes from the database, not merely from the expected context.
        return Observation(source, datetime.now(timezone.utc).isoformat(), data["run_id"],
                           context.operation_id, context.attempt_id, True, data)

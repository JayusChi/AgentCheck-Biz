from agentcheck_biz.adapters.contracts import Observation, RunContext
from agentcheck_biz.adapters.contracts import ObservationError
from agentcheck_biz.checks import check_run, load_json, verdict
from pathlib import Path
import sqlite3


def recheck_ticket_run(run_dir: Path) -> dict:
    """Accept V1 evidence; validate D17 envelopes before the unchanged oracle."""
    run_dir = Path(run_dir).resolve()
    try:
        run = load_json(run_dir / "run.json")
        proxy_check = None
        if run.get("proxy_enabled"):
            from agentcheck_biz.fault_proxy.verify import assess_proxy_evidence
            coverage = assess_proxy_evidence(run_dir, run)
            if coverage["status"] != "PASS":
                return verdict(coverage["status"], coverage["reason"], coverage["checks"])
            proxy_check = coverage["checks"][0]
        if "plugin" not in run or run.get("execution_status") != "completed":
            return check_run(run_dir)
        if run["plugin"] not in ({"name": "ticket-local", "version": 1}, {"name": "ticket-http", "version": 1},
                                 {"name": "ticket-mcp", "version": 1}):
            raise ObservationError("Unsupported saved ticket plugin identity")
        context = RunContext(**{**run["context"], "evidence_dir": run_dir})
        if context.run_id != run["run_id"] or run.get("cleanup_status") != "completed":
            raise ObservationError("Saved run identity or cleanup evidence is incomplete")
        for phase in ("initial", "final"):
            observation = Observation(**load_json(run_dir / f"observation-{phase}.json"))
            observation.require_complete(context)
            if (observation.source != "sqlite-readonly:business.sqlite"
                    or observation.data != load_json(run_dir / f"{phase}.json")):
                raise ObservationError("Observation source or legacy snapshot mismatch")
        http_check = None
        mcp_check = None
        from agentcheck_biz.commit_loss.verify import unknown_calls
        unknown = unknown_calls(run_dir, run)
        if run["plugin"]["name"] == "ticket-mcp":
            from .mcp import verify_mcp_evidence
            mcp_check = verify_mcp_evidence(run_dir, run, unknown)
        if run["plugin"]["name"] in {"ticket-http", "ticket-mcp"}:
            from .ticket_http import verify_http_evidence
            http_check = verify_http_evidence(run_dir, run, unknown)
        result = check_run(run_dir)
        if http_check is not None and result["status"] in {"PASS", "FAIL"}:
            result["checks"].append(http_check)
        if mcp_check is not None and result["status"] in {"PASS", "FAIL"}:
            result["checks"].append(mcp_check)
        if proxy_check is not None and result["status"] in {"PASS", "FAIL"}:
            result["checks"].extend(coverage["checks"])
        return result
    except ObservationError as error:
        return verdict(error.status, str(error), [])
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError, StopIteration) as error:
        return verdict("ERROR", f"Unable to read observation evidence: {type(error).__name__}: {error}", [])


class TicketBusinessVerifier:
    def verify(self, context: RunContext, observation: Observation) -> dict:
        observation.require_complete(context)
        # Retain the V1 oracle and its independent database re-read, including all
        # negative checks. Saved case/report formats and old runs remain usable.
        return recheck_ticket_run(context.evidence_dir)

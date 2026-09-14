"""A/B evidence demo. D4 will add the formal business checker and fixed version."""

import json
from pathlib import Path
from uuid import uuid4

from agentcheck_biz.events import EventLog
from agentcheck_biz.faults import ResponseLossOnce

from .database import connect_database, initialize_database
from .scripted_agent import run_scripted_client
from .service import OperationContext, TicketService
from .tool_executor import TicketToolExecutor


def read_rows(db_path: Path) -> list[dict]:
    with connect_database(db_path, readonly=True) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM tickets ORDER BY rowid")]


def run_phase(output_root: Path, *, inject_fault: bool) -> dict:
    phase = "faulted" if inject_fault else "clean"
    run_id = f"d3-{phase}-{uuid4().hex}"
    run_dir = Path(output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    db_path = run_dir / "business.sqlite"
    events = EventLog(run_dir / "events.jsonl", run_id)
    report = {
        "run_id": run_id, "phase": phase,
        "mode": "scripted fixture; no model; local tool-boundary simulation",
        "app_version": "D2 service without idempotency",
        "database": str(db_path.resolve()),
        "context": {"tenant_id": "tenant-A", "operation_id": "repair-001"},
        "max_client_attempts": 2,
        "fault": {"tool": "create_ticket", "occurrence": 1,
                  "point": "after_commit_before_result", "max_injections": 1}
        if inject_fault else None,
        "execution_status": "running",
    }
    events.record("run_started", phase=phase)
    try:
        initialize_database(db_path)
        initial_rows = read_rows(db_path)
        events.record("environment_ready", database=str(db_path.resolve()), rows=initial_rows)
        context = OperationContext(**report["context"])
        fault = ResponseLossOnce() if inject_fault else None
        executor = TicketToolExecutor(TicketService(db_path), context, events, fault)
        client_result = run_scripted_client(executor.create_ticket, events)
        final_rows = read_rows(db_path)
        events.record("final_state_observed", rows=final_rows)
        report.update(
            execution_status="completed", initial_rows=initial_rows,
            final_rows=final_rows, ticket_count=len(final_rows),
            client_result=client_result, tool_calls=executor.call_count,
            fault_triggered=bool(fault and fault.triggered),
        )
    except Exception as error:
        report.update(execution_status="error", error_type=type(error).__name__, error=str(error))
        events.record("run_error", error_type=type(error).__name__, message=str(error))
        raise
    finally:
        # Every DB operation has already closed its connection, including on errors.
        # Persisted DB files stay available as evidence; no background process is used.
        events.record("run_finished", execution_status=report["execution_status"])
        report["events_file"] = str(events.path.resolve())
        (run_dir / "run.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def main() -> int:
    output_root = Path(__file__).resolve().parents[2] / "artifacts"
    for inject_fault in (False, True):
        report = run_phase(output_root, inject_fault=inject_fault)
        print(f"\n[{report['phase']}] client={report['client_result']['status']}, "
              f"tool_calls={report['tool_calls']}, tickets={report['ticket_count']}, "
              f"fault_triggered={report['fault_triggered']}")
        for line in Path(report["events_file"]).read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            print(f"  {event['seq']:02d} {event['event']} "
                  f"{event.get('call_id', '')} {event.get('ticket_id', '')}")
        print(f"  Evidence: {Path(report['database']).parent}")
    print("\nD3 evidence captured. Two tickets in the faulted run demonstrate a business defect.")
    print("Exit 0 means the demo completed; it is NOT a business PASS verdict.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

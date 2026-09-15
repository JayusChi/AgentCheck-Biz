"""Run from the repository root: python -m examples.ticket_agent.demo."""

import json
from pathlib import Path
from uuid import uuid4

from .database import connect_database, initialize_database
from .service import OperationContext, TicketService


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    run_dir = repo_root / "artifacts" / ("d2-" + uuid4().hex)
    run_dir.mkdir(parents=True, exist_ok=False)
    db_path = run_dir / "business.sqlite"
    initialize_database(db_path)
    service = TicketService(db_path)
    context = OperationContext(tenant_id="tenant-A", operation_id="repair-001")
    created = service.create_ticket(
        context, customer_id="C001", device_id="D001", description="无法开机"
    )
    queried = service.query_tickets(context)

    # Independent observation: a NEW read-only connection, no service query here.
    with connect_database(db_path, readonly=True) as connection:
        observed = [dict(row) for row in connection.execute("SELECT * FROM tickets")]

    expected_fields = {
        "tenant_id": "tenant-A",
        "operation_id": "repair-001",
        "customer_id": "C001",
        "device_id": "D001",
        "description": "无法开机",
        "status": "open",
    }
    checks = {
        "exactly_one_persisted_ticket": len(observed) == 1,
        "persisted_fields_match_request": len(observed) == 1
        and all(observed[0][key] == value for key, value in expected_fields.items()),
        "returned_id_matches_database": len(observed) == 1
        and observed[0]["ticket_id"] == created["ticket_id"],
        "query_matches_database": queried == observed,
    }
    report = {
        "mode": "D2 direct service call; no model; no fault injection",
        "database": str(db_path),
        "created": created,
        "queried": queried,
        "independently_observed": observed,
        "checks": checks,
        "passed": all(checks.values()),
    }
    report_path = run_dir / "verification.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Report: {report_path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

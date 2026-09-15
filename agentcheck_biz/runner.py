"""V1-compatible ticket entry point backed by the registered TicketLocal plugin."""

from pathlib import Path

from .checks import load_json
from .lifecycle import run_business_case
from .provenance import REPO_ROOT, implementation_digest


def default_case() -> dict:
    return load_json(REPO_ROOT / "cases" / "tickets" / "D4_create.json")


def run_ticket_case(output_root: Path, *, app_version: str, inject_fault: bool,
                    case: dict | None = None, agent: str = "scripted", model_config=None) -> dict:
    return run_business_case(output_root, plugin_id="ticket-local", case=case if case is not None else default_case(),
                             options={"app_version": app_version, "inject_fault": inject_fault,
                                      "agent": agent, "model_config": model_config})

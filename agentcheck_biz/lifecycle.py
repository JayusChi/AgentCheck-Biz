"""Object-independent orchestration of registered business plugins."""

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

from .adapters.contracts import Observation, ObservationError, RunContext
from .adapters.registry import builtin_registry
from .events import EventLog
from .provenance import implementation_digest
from .reports import save_check_report, save_json


EXIT_CODES = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "ERROR": 3}


def failure_result(status, reason):
    return {"status": status, "exit_code": EXIT_CODES[status], "reason": reason, "checks": []}


def validate_result(result):
    if not isinstance(result, dict) or result.get("status") not in EXIT_CODES:
        raise ValueError("Verifier returned an invalid verdict")
    if (type(result.get("exit_code")) is not int or result["exit_code"] != EXIT_CODES[result["status"]]
            or not isinstance(result.get("reason"), str) or not result["reason"].strip()
            or not isinstance(result.get("checks"), list)):
        raise ValueError("Verifier returned malformed evidence")
    for check in result["checks"]:
        if (not isinstance(check, dict) or not {"check_id", "expected", "actual", "passed", "evidence"} <= check.keys()
                or type(check["passed"]) is not bool):
            raise ValueError("Verifier returned a malformed check")
    if result["status"] in {"PASS", "FAIL"}:
        if not result["checks"] or (result["status"] == "PASS") != all(check["passed"] for check in result["checks"]):
            raise ValueError("Verifier verdict contradicts its checks")
    json.dumps(result, allow_nan=False)
    return result


def run_business_case(output_root: Path, *, plugin_id: str, case: dict, options: dict | None = None, registry=None):
    # Configuration/capability errors happen before any environment is allocated.
    plugin = (registry if registry is not None else builtin_registry()).resolve(plugin_id, case, {} if options is None else options)
    run_id = plugin.run_prefix + "-" + uuid4().hex
    run_dir = Path(output_root).resolve() / run_id
    context = RunContext(run_id, plugin.operation_id, "attempt-" + uuid4().hex,
                         time.time() + plugin.timeout_seconds, run_dir)
    run_dir.mkdir(parents=True, exist_ok=False)
    events = EventLog(run_dir / "events.jsonl", run_id)
    save_json(run_dir / "case.json", plugin.case)
    run = {**plugin.run_metadata, "run_id": run_id,
           "execution_status": "preparing", "lifecycle_phase": "preparing",
           "plugin": {"name": plugin.name, "version": plugin.version}, "context": context.to_dict(),
           "implementation_sha256": implementation_digest(), "started_at": datetime.now(timezone.utc).isoformat(),
           "client_result": None, "tool_calls": 0, "cleanup_status": "pending"}
    events.record("run_started", plugin=plugin.name, app_version=run.get("app_version"))
    save_json(run_dir / "run.json", run)
    problem = None
    final_observation = None
    final_attempted = False

    def transition(status):
        run.update(execution_status=status, lifecycle_phase=status)
        events.record("state_changed", state=status)
        save_json(run_dir / "run.json", run)

    def fail(error, event="run_error"):
        nonlocal problem
        status = error.status if isinstance(error, ObservationError) else "INCONCLUSIVE" if isinstance(error, KeyboardInterrupt) else "ERROR"
        detail = {"error_type": type(error).__name__, "error": str(error) or "Run interrupted by user"}
        run.setdefault("errors", []).append({"event": event, **detail})
        events.record(event, error_type=detail["error_type"], message=detail["error"])
        if problem is None or status == "ERROR":
            problem = failure_result(status, f"{detail['error_type']}: {detail['error']}")
            run.update(**detail, execution_status="timed_out" if isinstance(error, TimeoutError)
                       else "error" if status == "ERROR" else "interrupted")

    def observe(phase):
        nonlocal final_attempted
        if phase == "final":
            final_attempted = True
        try:
            value = plugin.observer.observe(context)
            if not isinstance(value, Observation):
                raise TypeError("StateObserver must return a versioned Observation")
            json.dumps(value.to_dict(), allow_nan=False)
        except Exception as error:
            value = Observation.failure(context, plugin.name + ".observer", error)
        # Preserve incomplete/foreign observations before validation; never turn
        # them into an empty legacy snapshot or a successful business result.
        save_json(run_dir / f"observation-{phase}.json", value.to_dict())
        value.require_complete(context)
        save_json(run_dir / f"{phase}.json", value.data)
        events.record("environment_ready" if phase == "initial" else "final_state_observed",
                      source=value.source, snapshot=value.data)
        return value

    try:
        context.require_time()
        plugin.environment.prepare(context, events)
        observe("initial")
        context.require_time()
        transition("running")
        run["client_result"] = plugin.execution.execute(context, events)
        context.require_time()
        transition("observing")
        final_observation = observe("final")
        run["execution_status"] = "completed"
    except KeyboardInterrupt as error:
        fail(error, "run_interrupted")
    except Exception as error:
        fail(error)
    finally:
        # Capture visible effects after execution failure without hiding it.
        if not final_attempted:
            try:
                final_observation = observe("final")
            except (Exception, KeyboardInterrupt) as error:
                fail(error, "observation_error")
        try:
            metadata = plugin.execution.metadata()
            json.dumps(metadata, allow_nan=False)
            if (not isinstance(metadata, dict) or set(metadata) & {"run_id", "context", "plugin", "execution_status",
                    "lifecycle_phase", "errors", "error", "error_type", "cleanup_status", "client_result", "implementation_sha256"}
                    or type(metadata.get("tool_calls")) is not int or metadata["tool_calls"] < 0):
                raise ValueError("Invalid ExecutionAdapter metadata")
            run.update(metadata)
        except (Exception, KeyboardInterrupt) as error:
            fail(error, "execution_metadata_error")
        try:
            plugin.environment.cleanup(context, events)
            run["cleanup_status"] = "completed"
            events.record("environment_cleaned", evidence_retained=True)
        except (Exception, KeyboardInterrupt) as error:
            run["cleanup_status"] = "error"
            fail(error, "cleanup_error")
        run["finished_at"] = datetime.now(timezone.utc).isoformat()
        events.record("run_finished", execution_status=run["execution_status"])
        run["lifecycle_phase"] = "checking"
        save_json(run_dir / "run.json", run)
    if problem is not None:
        result = problem
    else:
        try:
            result = validate_result(plugin.verifier.verify(context, final_observation))
        except (Exception, KeyboardInterrupt) as error:
            # Execution events are sealed; persist the verifier error separately.
            interrupted = isinstance(error, KeyboardInterrupt)
            run.update(execution_status="interrupted" if interrupted else "error", error_type=type(error).__name__, error=str(error))
            save_json(run_dir / "run.json", run)
            result = failure_result("INCONCLUSIVE" if interrupted else "ERROR", f"Verifier error: {type(error).__name__}: {error}")
    save_check_report(run_dir, result)
    run.update(business_status=result["status"], lifecycle_phase="finished")
    save_json(run_dir / "run.json", run)
    return {"run_dir": str(run_dir), "run": run, "result": result}

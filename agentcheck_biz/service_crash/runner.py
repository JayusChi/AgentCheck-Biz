"""Crash at a proven point, restart, and continue the same D23 policy/budget."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import time
from uuid import uuid4

from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.checks import load_json
from agentcheck_biz.commit_loss.barrier import wait_message
from agentcheck_biz.events import EventLog
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.recovery.policy import Budget, Contract, Outcome, RecoveryPolicy
from agentcheck_biz.recovery.ticket_http import TicketRecoveryAdapter
from agentcheck_biz.reports import save_json
from . import POINTS, VERSION
from .barrier import publish
from .observer import observe
from .runtime import CrashRuntime


class ObservationUnavailable(RuntimeError):
    pass


def require_observation(context, phase, events):
    result = observe(context, phase, events)
    if not result["complete"]:
        raise ObservationUnavailable(phase + ": " + result["error"])
    return result


def run_case(output, point, *, max_tool_calls=6, timeout_seconds=45):
    if point not in POINTS:
        raise ValueError("Unsupported service crash point")
    if type(max_tool_calls) is not int or max_tool_calls < 1 or not 0 < timeout_seconds <= 120:
        raise ValueError("Invalid crash experiment budget")
    run_id = "service-crash-" + uuid4().hex
    directory = Path(output).resolve() / run_id
    directory.mkdir(parents=True)
    case = load_json(REPO_ROOT / "cases/tickets/full/T01.json")
    context = RunContext(run_id, case["context"]["operation_id"], "attempt-" + uuid4().hex,
                         time.time() + timeout_seconds, directory)
    events = EventLog(directory / "events.jsonl", run_id)
    trace = EventLog(directory / "recovery-events.jsonl", run_id)
    budget = Budget(context.deadline, max_tool_calls, 0)
    policy = RecoveryPolicy(case["context"], case["request"], Contract("ticket-http/1:fixed", True, (), (403, 409, 422)),
        budget, attempt_prefix=context.attempt_id, record=lambda row: trace.record("recovery", row=row))
    manifest = {"schema_version": 1, "version": VERSION, "run_id": run_id, "context": context.to_dict(),
                "point": point, "case": case, "runner_pid": os.getpid(), "model_requests": 0,
                "timeout_seconds": timeout_seconds, "implementation_sha256": implementation_digest()}
    save_json(directory / "crash-run.json", manifest)
    runtime = CrashRuntime(context, case, events)
    execution = {"status": "running"}
    client = None
    try:
        runtime.start(point)
        require_observation(context, "initial", events)
        adapter = TicketRecoveryAdapter(context, runtime, runtime.origin,
            policy.contract, EventLog(directory / "http-client.jsonl", run_id))

        def call(action, scope, request, attempt_id, remaining):
            events.record("client_route", instance_id=runtime.current.instance_id,
                          generation=runtime.current.generation, origin=runtime.origin,
                          action=action, attempt_id=attempt_id, **scope, budget=budget.snapshot())
            if adapter.calls:
                return adapter(action, scope, request, attempt_id, remaining)
            expected = runtime.arm(point, attempt_id)
            if point == "before_service":
                # This parent gate has not dispatched any business HTTP request.
                barrier = {**expected, "publisher_pid": os.getpid(), "gate": "before_http_dispatch"}
                publish(runtime.current.directory / "barrier.json", barrier)
                events.record("request_gate_held", barrier=barrier)
                require_observation(context, "at_barrier", events)
                runtime.crash(expected, barrier)
                # The gate cancels dispatch while the owned service is killed.
                # No reconnection to a freed port and no synthetic HTTP failure.
                adapter.calls += 1
                receipt = events.record("client_not_dispatched", attempt_id=attempt_id, call_id="call-1",
                                        instance_id=runtime.current.instance_id, reason="before_service_gate")
                result = Outcome("not_forwarded", evidence=f"events.jsonl#{receipt['seq']}")
            else:
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crash-http")
                future = executor.submit(adapter, action, scope, request, attempt_id, remaining)
                try:
                    barrier = wait_message(runtime.current.directory / "barrier.json", expected,
                                           min(context.deadline, time.time() + 8))
                    if future.done():
                        raise RuntimeError("Client completed before crash; exact loss not covered")
                    require_observation(context, "at_barrier", events)
                    runtime.crash(expected, barrier)
                    result = future.result(timeout=6)
                finally:
                    # Also unblocks a held transaction if the gate/checker raised.
                    if runtime.current.process.poll() is None:
                        runtime.stop(runtime.current, "failed_gate_cleanup")
                    executor.shutdown(wait=True, cancel_futures=True)
            if (result.kind != ("not_forwarded" if point == "before_service" else "outcome_unknown")
                    or result.evidence.endswith(":TimeoutError")):
                raise RuntimeError("Crash did not produce the required real connection failure")
            events.record("crashed_call_returned", attempt_id=attempt_id, kind=result.kind, evidence=result.evidence,
                          budget=budget.snapshot())
            # A hot SQLite rollback journal can make a strictly readonly open fail
            # before recovery. Save that failure; never turn it into an empty read.
            observe(context, "after_exit", events)
            runtime.start()
            require_observation(context, "after_restart", events)
            adapter.origin = runtime.origin
            events.record("recovery_resumed", operation_id=context.operation_id, instance_id=runtime.current.instance_id,
                          budget=budget.snapshot())
            return result

        client = policy.run(call)
        save_json(directory / "client.json", client)
        require_observation(context, "final", events)
        execution = {"status": "completed"}
    except Exception as error:
        execution = {"status": "error", "error_type": type(error).__name__, "error": str(error)}
        events.record("experiment_failed", **execution)
        if not (directory / "observation-final.json").exists():
            observe(context, "final", events)
    finally:
        try:
            runtime.close()
        except Exception as error:
            execution = {"status": "error", "error_type": type(error).__name__, "error": str(error)}
        save_json(directory / "execution.json", execution)
    from .verify import recheck
    check = recheck(directory)
    save_json(directory / "crash-checks.json", check)
    return {"run_dir": str(directory), "point": point, "tool_calls": budget.tool_calls,
            "client_status": client["status"] if client else "needs_verification", **check}

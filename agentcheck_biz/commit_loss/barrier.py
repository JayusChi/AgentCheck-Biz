"""Local atomic-file handshakes. Polls wait for evidence, never infer a commit."""

import asyncio
import json
import os
from pathlib import Path
import threading
import time

from agentcheck_biz.checks import observe_database
from agentcheck_biz.events import EventLog
from agentcheck_biz.reports import save_json
from agentcheck_biz.observers.gitea import normalize_issue
from agentcheck_biz.gitea_cases import issue_body

KEYS = ("run_id", "nonce", "kind", "call_id", "request_id", "attempt_id", "operation_id")


def read_message(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError):
        return None


def bound(message, expected):
    if not isinstance(message, dict) or any(message.get(k) != expected[k] for k in expected):
        raise ValueError("Foreign or incomplete barrier identity")
    return message


def wait_message(path, expected, deadline, stop=None):
    while time.time() < deadline and not (stop and stop.is_set()):
        value = read_message(path)
        if value is not None:
            return bound(value, expected)
        if stop:
            stop.wait(.01)
        else:
            time.sleep(.01)
    raise TimeoutError("Barrier evidence did not arrive before its deadline")


async def wait_message_async(path, expected, deadline):
    while time.time() < deadline:
        value = read_message(path)
        if value is not None:
            return bound(value, expected)
        await asyncio.sleep(.01)
    raise TimeoutError("Barrier decision did not arrive before its deadline")


def ticket_barrier(config, request, receipt, events):
    barrier = config.get("test_commit_barrier")
    if not barrier or request.headers["x-request-id"] != "call-1":
        return
    directory = Path(config["evidence_dir"])
    identity = {**barrier, "run_id": config["run_id"], "call_id": "call-1",
                "request_id": "call-1", "attempt_id": request.headers["x-attempt-id"],
                "operation_id": request.headers["x-operation-id"]}
    ready = {**identity, "pid": os.getpid(), "monotonic_ns": time.monotonic_ns(),
             "ticket": receipt.ticket, "deduplicated": receipt.deduplicated}
    events.record("commit_barrier_waiting", **ready)
    save_json(directory / "commit-loss-ticket-ready.json", ready)
    release = wait_message(directory / "commit-loss-release.json", identity, min(config["deadline"], time.time() + 10))
    events.record("commit_barrier_released", **identity, pid=os.getpid(),
                  monotonic_ns=time.monotonic_ns(), confirmed=release["confirmed"])


class Controller:
    def __init__(self, context, config, environment, case):
        self.context, self.config, self.environment, self.case = context, config, environment, case
        self.stop = threading.Event()
        self.log = EventLog(context.evidence_dir / "commit-loss-events.jsonl", context.run_id)
        self.thread = threading.Thread(target=self.run, name="commit-loss-controller", daemon=True)

    def record(self, event, **data):
        self.log.record(event, pid=os.getpid(), monotonic_ns=time.monotonic_ns(), **data)

    def confirm(self, ready, identity, deadline):
        directory = self.context.evidence_dir
        if self.config["kind"] == "sqlite_commit":
            receipt = wait_message(directory / "commit-loss-ticket-ready.json", identity, deadline, self.stop)
            if receipt["pid"] != self.environment.identity["pid"] or receipt["deduplicated"]:
                raise ValueError("Expected an owned service's new commit receipt")
            snapshot = observe_database(directory / "business.sqlite")  # new mode=ro connection
            ticket = receipt["ticket"]
            if (snapshot["run_id"] != self.context.run_id or ticket not in snapshot["tickets"]
                    or any(ticket[k] != v for k, v in {**self.case["context"], **self.case["request"]}.items())):
                raise ValueError("Committed ticket not independently visible in bound scope")
            return {"source": "sqlite-readonly:business.sqlite", "record": ticket, "snapshot": snapshot,
                    "service_commit_monotonic_ns": receipt["monotonic_ns"]}
        response = read_message(directory / "commit-loss-upstream.json")
        bound(response, identity)
        repository = self.environment.repository
        ticket = normalize_issue(response["body"], repository)
        expected_body = issue_body(self.context, self.context.operation_id, self.case["request"]["body"])
        if response["status_code"] != 201 or ticket["title"] != self.case["request"]["title"] or ticket["body"] != expected_body:
            raise ValueError("Upstream Issue does not match bound operation")
        # Existing read-only credential remains solely in the controller process.
        detail = self.environment.observer_client.request("GET", self.environment.repo_path + f"/issues/{ticket['number']}")
        if normalize_issue(detail["body"], repository) != ticket:
            raise ValueError("Independent API confirmation differs from upstream response")
        return {"source": "gitea-api-readonly", "record": ticket, "api_evidence": detail["evidence"]}

    def run(self):
        directory = self.context.evidence_dir
        expected = {**self.config, "run_id": self.context.run_id}
        identity = None
        confirmed = False
        try:
            ready = wait_message(directory / "commit-loss-proxy-ready.json", expected, self.context.deadline, self.stop)
            identity = {k: ready[k] for k in KEYS}
            if (ready["operation_id"] != self.context.operation_id or ready["call_id"] != "call-1"
                    or not ready["attempt_id"].startswith(self.context.attempt_id + "/call-1-")):
                raise ValueError("Barrier request differs from root operation/attempt")
            deadline = min(self.context.deadline, time.time() + 8)
            self.record("confirmation_requested", **identity)
            proof = self.confirm(ready, identity, deadline)
            self.record("commit_confirmed" if self.config["kind"] == "sqlite_commit" else "api_visibility_confirmed",
                        **identity, **proof)
            decision = {**identity, **proof, "confirmed": True, "pid": os.getpid(), "monotonic_ns": time.monotonic_ns()}
            save_json(directory / "commit-loss-decision.json", decision)
            confirmed = True
            ack = wait_message(directory / "commit-loss-aborted.json", identity, deadline, self.stop)
            if ack.get("aborted") is not True:
                raise ValueError("Proxy did not acknowledge a real abort")
            self.record("abort_acknowledged", **identity, proxy_pid=ack["pid"])
        except Exception as error:
            self.record("confirmation_failed", error_type=type(error).__name__)
            if identity is not None and not confirmed:
                save_json(directory / "commit-loss-decision.json", {**identity, "confirmed": False,
                    "pid": os.getpid(), "monotonic_ns": time.monotonic_ns(), "error_type": type(error).__name__})
        finally:
            if identity is not None and self.config["kind"] == "sqlite_commit":
                save_json(directory / "commit-loss-release.json", {**identity, "confirmed": confirmed})
            self.record("controller_stopped", confirmed=confirmed)

    def close(self):
        self.stop.set()
        self.thread.join(6)
        if self.thread.is_alive():
            raise RuntimeError("Commit-loss controller did not stop")

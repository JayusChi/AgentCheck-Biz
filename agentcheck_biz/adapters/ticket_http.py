"""D18 environment-owned HTTP service and bounded deterministic execution."""

from dataclasses import asdict
import json
import os
import platform
import secrets
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from agentcheck_biz.cases import validate_case
from agentcheck_biz.observers.ticket_http import TicketHttpObserver
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.reports import save_json
from agentcheck_biz.verifiers.ticket_local import TicketBusinessVerifier
from examples.ticket_agent.fixed_service import IdempotencyConflict
from examples.ticket_agent.scripted_agent import run_scripted_client
from examples.ticket_agent.service import OperationContext
from examples.ticket_agent.tool_executor import ToolBudgetExceeded
from examples.ticket_http import API_VERSION
from .contracts import BusinessPlugin, PluginConfigurationError
from .http_transport import HttpTimeouts, HttpTransport


class TicketHttpEnvironment:
    def __init__(self, case, app_version, timeouts):
        self.case, self.app_version, self.timeouts = case, app_version, timeouts
        self.process = self.process_pid = self.owner_pid = self.log = self.transport = self.identity = None
        self.tokens = {}
        self.instance_id = uuid4().hex

    def prepare(self, context, events):
        contexts = [self.case["context"]] + ([self.case["secondary_context"]] if "secondary_context" in self.case else [])
        self.tokens = {row["tenant_id"]: "Bearer " + secrets.token_urlsafe(32) for row in contexts}
        config = {"case": self.case, "run_id": context.run_id, "evidence_dir": str(context.evidence_dir),
                  "app_version": self.app_version, "instance_id": self.instance_id,
                  "parent_pid": os.getpid(), "deadline": context.deadline,
                  "identities": {token: tenant for tenant, token in self.tokens.items()}}
        if getattr(self, "test_commit_barrier", None):
            config["test_commit_barrier"] = self.test_commit_barrier
        # An allowlist keeps provider credentials and proxy settings out of the child.
        keep = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "SYSTEMDRIVE", "PATHEXT"}
        env = {key: value for key, value in os.environ.items() if key.upper() in keep}
        env.update(PYTHONNOUSERSITE="1", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        executable = sys.executable
        if sys.platform == "win32":
            # Windows venv python.exe is a redirector spawning another process.
            # Launch the interpreter directly with CPython's venv launcher hint
            # so the owned Popen handle is the actual service, with the same venv.
            executable = sys._base_executable
            env["__PYVENV_LAUNCHER__"] = sys.executable
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        self.log = (context.evidence_dir / "http-service.log").open("wb")
        self.owner_pid = os.getpid()
        self.process = subprocess.Popen([executable, "-X", "utf8", "-m", "examples.ticket_http.server"],
            cwd=REPO_ROOT, env=env, stdin=subprocess.PIPE, stdout=self.log, stderr=subprocess.STDOUT, **flags)
        self.process_pid = self.process.pid
        events.record("http_process_created", pid=self.process_pid, owner_pid=self.owner_pid,
                      instance_id=self.instance_id)
        try:
            self.process.stdin.write((json.dumps(config, ensure_ascii=False) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        finally:
            self.process.stdin.close()
        ready = context.evidence_dir / "http-ready.json"
        startup_deadline = min(context.deadline, time.time() + 12)
        while not ready.exists():
            if self.process.poll() is not None:
                raise RuntimeError("HTTP service startup failed; see http-service.log")
            if time.time() >= startup_deadline:
                raise TimeoutError("HTTP service readiness deadline exhausted")
            time.sleep(.03)
        identity = json.loads(ready.read_text(encoding="utf-8"))
        expected = {"service": "ticket-http", "api_version": API_VERSION, "app_version": self.app_version,
                    "run_id": context.run_id, "instance_id": self.instance_id, "pid": self.process_pid,
                    "host": "127.0.0.1", "implementation_sha256": implementation_digest()}
        if (any(identity.get(key) != value for key, value in expected.items())
                or type(identity.get("port")) is not int or not 0 < identity["port"] <= 65535
                or identity["pid"] == self.owner_pid):
            raise RuntimeError("HTTP service identity or source version mismatch")
        self.identity = identity
        self.transport = HttpTransport(f"http://127.0.0.1:{identity['port']}", context, events, self.timeouts)
        token = self.tokens[self.case["context"]["tenant_id"]]
        probes = {}
        for path in ("/health", "/version"):
            status, data = self.transport.request("GET", path, token=token, request_id="probe-" + path[1:])
            probes[path] = {"status_code": status, "body": data}
            save_json(context.evidence_dir / "http-probes.json", probes)
            wanted = {"status": "ok", **identity} if path == "/health" else identity
            if status != 200 or data != wanted:
                raise RuntimeError("HTTP health/version probe disagrees with the owned process")
        events.record("http_service_verified", identity=identity)

    def cleanup(self, context, events):
        try:
            if self.process is not None:
                # Only terminate the Popen handle allocated by this environment.
                # No PID scans, port-based kills or attachment to existing services.
                if self.owner_pid != os.getpid() or self.process.pid != self.process_pid:
                    raise RuntimeError("Refusing cleanup of an unowned process")
                if self.identity is not None and (self.identity["pid"] != self.process_pid
                        or self.identity["run_id"] != context.run_id or self.identity["instance_id"] != self.instance_id):
                    raise RuntimeError("Refusing cleanup: service identity changed")
                already_exited = self.process.poll() is not None
                if not already_exited:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
                save_json(context.evidence_dir / "http-cleanup.json", {
                    "run_id": context.run_id, "instance_id": self.instance_id, "pid": self.process_pid,
                    "owner_pid": self.owner_pid, "exited": self.process.poll() is not None,
                    "already_exited": already_exited, "returncode": self.process.returncode})
                events.record("http_process_stopped", pid=self.process_pid, instance_id=self.instance_id)
        finally:
            self.tokens.clear()
            if self.log is not None:
                self.log.close()


class TicketHttpToolExecutor:
    def __init__(self, transport, token, operation, context, events, allocate):
        self.transport, self._token, self.operation = transport, token, operation
        self.run_context, self.events, self.allocate = context, events, allocate

    def _call(self, tool, method, body=None):
        call_id = self.allocate()
        self.events.record("tool_called", call_id=call_id, tool=tool,
                           tenant_id=self.operation.tenant_id, operation_id=self.operation.operation_id,
                           attempt_id=self.run_context.attempt_id + "/" + call_id, deadline=self.run_context.deadline)
        status, result = self.transport.request(method, "/tickets", token=self._token, request_id=call_id,
                                               operation_id=self.operation.operation_id, body=body)
        if status == 409:
            raise IdempotencyConflict("HTTP service rejected conflicting operation content")
        if status != 200:
            raise RuntimeError(f"HTTP ticket service returned {status}")
        self.events.record("tool_result_delivered", call_id=call_id,
                           **({"result": result} if method == "POST" else {"tickets": result}))
        return result

    def create_ticket(self, *, customer_id, device_id, description):
        # No tenant / credential / operation / URL arguments in the tool schema.
        return self._call("create_ticket", "POST", dict(customer_id=customer_id, device_id=device_id, description=description))

    def query_tickets(self):
        return self._call("query_tickets", "GET")


class TicketHttpExecution:
    def __init__(self, environment, case):
        self.environment, self.case, self.call_count = environment, case, 0

    def execute(self, context, events):
        def allocate():
            context.require_time()
            if self.call_count >= self.case["limits"]["max_tool_calls"]:
                events.record("tool_budget_exceeded", maximum=self.case["limits"]["max_tool_calls"])
                raise ToolBudgetExceeded("HTTP tool call budget exhausted")
            self.call_count += 1
            return f"call-{self.call_count}"

        def bound(scope):
            operation = OperationContext(**scope)
            return TicketHttpToolExecutor(self.environment.transport, self.environment.tokens[operation.tenant_id],
                                          operation, context, events, allocate)

        primary = bound(self.case["context"])
        scenario, fields = self.case.get("scenario", "retry"), self.case["request"]
        if scenario in {"tenant_isolation", "distinct_operations"}:
            # Two separately bound test clients; a tool call cannot switch tenants.
            secondary = bound(self.case["secondary_context"])
            first, second = primary.create_ticket(**fields), secondary.create_ticket(**fields)
            return {"status": "completed", "ticket_id": first["ticket_id"], "secondary_ticket_id": second["ticket_id"],
                    "primary_query": primary.query_tickets(), "secondary_query": secondary.query_tickets()}
        if scenario == "conflict":
            try:
                result = primary.create_ticket(**fields)
            except IdempotencyConflict:
                events.record("client_stopped", reason="idempotency_conflict")
                return {"status": "blocked", "ticket_id": None, "reason": "idempotency_conflict"}
            return {"status": "completed", "ticket_id": result["ticket_id"]}
        return run_scripted_client(primary.create_ticket, events, fields,
                                   max_attempts=self.case["limits"].get("max_client_attempts", 2))

    def metadata(self):
        transport = self.environment.transport
        return {"tool_calls": self.call_count,
                "http_request_attempts": transport.attempts if transport else 0,
                "http_business_request_attempts": transport.business_attempts if transport else 0,
                "http_service": self.environment.identity}


def ticket_http_plugin(case, options):
    if set(options) - {"app_version", "timeouts"}:
        raise PluginConfigurationError("Unknown TicketHttp options")
    config = validate_case(case)
    version = options.get("app_version", "fixed")
    if version not in {"unsafe", "fixed"} or config["fault"] is not None or config.get("scenario", "retry") not in {
            "retry", "replay", "tenant_isolation", "distinct_operations", "conflict"}:
        raise PluginConfigurationError("D18 supports clean scripted create/replay/isolation/conflict cases only")
    raw_timeouts = options.get("timeouts", {})
    if not isinstance(raw_timeouts, dict) or set(raw_timeouts) - {"connect", "read", "total"}:
        raise PluginConfigurationError("Invalid HTTP timeout options")
    timeouts = HttpTimeouts(**raw_timeouts)
    environment = TicketHttpEnvironment(config, version, timeouts)
    metadata = {"schema_version": 1, "case_id": config["case_id"], "app_version": version,
                "agent": "scripted", "model_called": False, "database": "business.sqlite",
                "python_version": platform.python_version(), "sqlite_version": sqlite3.sqlite_version,
                "runner_pid": os.getpid(), "transport": "http", "http_timeouts": asdict(timeouts),
                "http_auto_retries": 0, "mode": "受控客户端；未调用模型；独立本机 HTTP 工单服务；无故障注入"}
    return BusinessPlugin("ticket-http", 1, config, config["context"]["operation_id"], 45,
                          f"biz-http-{version}-clean", metadata, environment, TicketHttpExecution(environment, config),
                          TicketHttpObserver(), TicketBusinessVerifier())

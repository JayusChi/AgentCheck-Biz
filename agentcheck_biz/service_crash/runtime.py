"""Allocate and stop only owned process handles, with per-generation evidence."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import uuid4

from agentcheck_biz.adapters.http_transport import HttpTimeouts, HttpTransport
from agentcheck_biz.checks import load_json
from agentcheck_biz.commit_loss.barrier import bound, wait_message
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.reports import save_json
from .barrier import publish


@dataclass(frozen=True)
class Allocation:
    process: object
    pid: int
    owner_pid: int
    instance_id: str
    generation: int
    nonce: str
    directory: Path
    data_dir: Path
    log: object


class CrashRuntime:
    def __init__(self, context, case, events):
        self.context, self.case, self.events = context, case, events
        self.data_dir = context.evidence_dir / "data"
        self.data_dir.mkdir(exist_ok=False)
        (context.evidence_dir / "instances").mkdir()
        self.allocations = []
        self.current = self.identity = None
        self.origin = None
        self.tokens = {scope["tenant_id"]: "Bearer " + secrets.token_urlsafe(32)
                       for scope in [case["context"]]}
        self.closed = False

    def start(self, point=None):
        self.context.require_time()
        if self.closed or self.current and self.current.process.poll() is None:
            raise RuntimeError("Restart requires the previous owned process to have exited")
        generation = len(self.allocations) + 1
        if generation > 2:
            raise RuntimeError("D24 permits one bounded restart")
        instance_id, nonce = uuid4().hex, uuid4().hex
        directory = self.context.evidence_dir / "instances" / instance_id
        directory.mkdir()
        config = {"run_id": self.context.run_id, "root_dir": str(self.context.evidence_dir),
                  "evidence_dir": str(self.data_dir), "data_dir": str(self.data_dir),
                  "instance_dir": str(directory), "instance_id": instance_id, "nonce": nonce,
                  "generation": generation, "owner_pid": os.getpid(), "deadline": self.context.deadline,
                  "point": point, "case": self.case, "app_version": "fixed",
                  "identities": {token: tenant for tenant, token in self.tokens.items()}}
        keep = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "SYSTEMDRIVE", "PATHEXT"}
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        env.update(PYTHONNOUSERSITE="1", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        executable = sys.executable
        if sys.platform == "win32":
            executable = sys._base_executable
            env["__PYVENV_LAUNCHER__"] = sys.executable
        log = (directory / "process.log").open("wb")
        try:
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            process = subprocess.Popen([executable, "-X", "utf8", "-m", "agentcheck_biz.service_crash.server"],
                cwd=REPO_ROOT, env=env, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, **flags)
        except BaseException:
            log.close()
            raise
        allocation = Allocation(process, process.pid, os.getpid(), instance_id, generation, nonce,
                                directory, self.data_dir, log)
        self.allocations.append(allocation)
        self.current, self.identity = allocation, None
        public = {k: config[k] for k in ("run_id", "instance_id", "generation", "nonce", "data_dir", "owner_pid", "deadline", "point")}
        public.update(pid=process.pid, instance_dir=str(directory))
        save_json(directory / "allocation.json", public)
        self.events.record("process_allocated", **public)
        try:
            process.stdin.write((json.dumps(config, ensure_ascii=False) + "\n").encode("utf-8"))
            process.stdin.flush()
        finally:
            process.stdin.close()
        deadline = min(self.context.deadline, time.time() + 10)
        while not (directory / "ready.json").exists():
            if process.poll() is not None:
                raise RuntimeError("Service startup failed; see " + str(directory / "process.log"))
            if time.time() >= deadline:
                raise TimeoutError("Readiness budget exhausted")
            time.sleep(.01)
        identity = load_json(directory / "ready.json")
        expected = {k: public[k] for k in ("run_id", "instance_id", "generation", "nonce", "data_dir", "owner_pid", "pid")}
        bound(identity, {**expected, "host": "127.0.0.1", "service": "ticket-http", "app_version": "fixed",
                         "api_version": 1, "implementation_sha256": implementation_digest()})
        if type(identity["port"]) is not int or not 0 < identity["port"] < 65536:
            raise ValueError("Invalid allocated port")
        self.identity = identity
        self.origin = f"http://127.0.0.1:{identity['port']}"
        transport = HttpTransport(self.origin, self.context, self.events, HttpTimeouts())
        probes = {}
        for path in ("/health", "/version"):
            code, value = transport.request("GET", path, token=self.tokens[self.case["context"]["tenant_id"]],
                request_id=f"generation-{generation}-{path[1:]}")
            probes[path] = {"status_code": code, "body": value}
            save_json(directory / "probes.json", probes)
            if code != 200 or value != ({"status": "ok", **identity} if path == "/health" else identity):
                raise RuntimeError("Health/version identity mismatch")
        stat = (self.data_dir / "business.sqlite").stat()
        self.events.record("generation_healthy", identity=identity, database_file_id=[stat.st_dev, stat.st_ino])
        return allocation

    def assert_owned(self, allocation):
        if (allocation not in self.allocations or allocation.owner_pid != os.getpid()
                or allocation.process.pid != allocation.pid
                or allocation.data_dir.resolve() != self.context.evidence_dir / "data"
                or allocation.directory.resolve() != self.context.evidence_dir / "instances" / allocation.instance_id):
            raise ValueError("Refusing operation on a foreign process allocation")

    def arm(self, point, attempt_id, call_id="call-1"):
        allocation = self.current
        self.assert_owned(allocation)
        expected = {"run_id": self.context.run_id, "instance_id": allocation.instance_id,
                    "generation": allocation.generation, "nonce": allocation.nonce, "pid": allocation.pid,
                    "data_dir": str(allocation.data_dir), "point": point, "attempt_id": attempt_id,
                    "operation_id": self.context.operation_id, "call_id": call_id}
        publish(allocation.directory / "armed.json", expected)
        self.events.record("crash_armed", **expected)
        return expected

    def crash(self, expected, barrier):
        allocation = self.current
        self.assert_owned(allocation)
        bound(barrier, expected)
        bound(load_json(allocation.directory / "armed.json"), expected)
        if self.identity != load_json(allocation.directory / "ready.json"):
            raise ValueError("Refusing crash after readiness identity changed")
        bound(self.identity, {k: expected[k] for k in ("run_id", "instance_id", "generation", "nonce", "pid", "data_dir")})
        if allocation.process.poll() is not None:
            raise RuntimeError("Process already exited; crash point not covered")
        if (allocation.directory / "release.json").exists():
            raise ValueError("Barrier was released; exact crash point no longer proven")
        self.events.record("crash_identity_verified", identity=self.identity, barrier=barrier)
        self.stop(allocation, "fault_kill")

    def stop(self, allocation, reason):
        self.assert_owned(allocation)
        already_exited = allocation.process.poll() is not None
        self.events.record("process_stop_requested", instance_id=allocation.instance_id,
                           pid=allocation.pid, generation=allocation.generation, reason=reason,
                           already_exited=already_exited)
        if not already_exited:
            # SIGKILL on POSIX / TerminateProcess on Windows, through owned Popen
            # handle. Never enumerate process names or kill a PID supplied by a file.
            allocation.process.kill()
        allocation.process.wait(timeout=4)
        receipt = {"run_id": self.context.run_id, "instance_id": allocation.instance_id,
                   "generation": allocation.generation, "nonce": allocation.nonce, "pid": allocation.pid,
                   "owner_pid": allocation.owner_pid, "data_dir": str(allocation.data_dir),
                   "exited": allocation.process.poll() is not None, "returncode": allocation.process.returncode,
                   "reason": reason, "already_exited": already_exited}
        save_json(allocation.directory / "exit.json", receipt)
        self.events.record("process_exited", **receipt)
        allocation.log.close()

    def close(self):
        failures = []
        for allocation in self.allocations:
            try:
                if not (allocation.directory / "exit.json").exists() or allocation.process.poll() is None:
                    self.stop(allocation, "finally_cleanup")
            except Exception as error:
                failures.append(type(error).__name__ + ": " + str(error))
            finally:
                if allocation.process.poll() is not None:
                    allocation.log.close()
        self.tokens.clear()
        self.closed = True
        receipt = {"run_id": self.context.run_id, "allocated": len(self.allocations),
                   "exited": sum(a.process.poll() is not None for a in self.allocations),
                   "failures": failures, "proxy_processes_allocated": 0}
        save_json(self.context.evidence_dir / "cleanup.json", receipt)
        if failures or receipt["allocated"] != receipt["exited"]:
            raise RuntimeError("Owned service cleanup incomplete")

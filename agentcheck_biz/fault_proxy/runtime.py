"""Own a dedicated proxy process, pinned to the already prepared test target."""

from dataclasses import asdict
import json
import os
import subprocess
import sys
import time
from uuid import uuid4

from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.reports import save_json
from . import PROXY_VERSION
from .rules import loopback, resolve_rule


class ProxyRuntime:
    def __init__(self, context, events, target, rule):
        self.context, self.events = context, events
        self.target = target
        loopback(target["origin"])
        self.rule = resolve_rule(rule, context.run_id)
        self.process = self.log = self.identity = None
        self.nonce = uuid4().hex
        self.owner_pid = os.getpid()
        self.closed = False

    def start(self):
        if self.process is not None or self.closed:
            raise RuntimeError("Proxy runtime cannot be reused")
        ctx = self.context
        ctx.require_time()
        config = {"context": {**asdict(ctx), "evidence_dir": str(ctx.evidence_dir)}, "target": self.target,
                  "rule": self.rule, "nonce": self.nonce, "parent_pid": self.owner_pid}
        save_json(ctx.evidence_dir / "proxy-rule.json", self.rule)
        save_json(ctx.evidence_dir / "proxy-target.json", self.target)
        keep = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "SYSTEMDRIVE", "PATHEXT"}
        env = {key: value for key, value in os.environ.items() if key.upper() in keep}
        env.update(PYTHONNOUSERSITE="1", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        executable = sys.executable
        if sys.platform == "win32":
            executable = sys._base_executable
            env["__PYVENV_LAUNCHER__"] = sys.executable
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        self.log = (ctx.evidence_dir / "proxy-process.log").open("wb")
        self.process = subprocess.Popen([executable, "-X", "utf8", "-m", "agentcheck_biz.fault_proxy.server"],
            cwd=REPO_ROOT, env=env, stdin=subprocess.PIPE, stdout=self.log, stderr=subprocess.STDOUT, **flags)
        self.events.record("proxy_process_created", pid=self.process.pid, owner_pid=self.owner_pid, nonce=self.nonce)
        try:
            self.process.stdin.write((json.dumps(config, ensure_ascii=False) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        finally:
            self.process.stdin.close()
        ready = ctx.evidence_dir / "proxy-ready.json"
        deadline = min(ctx.deadline, time.time() + 8)
        while True:
            if self.process.poll() is not None:
                raise RuntimeError("Proxy startup failed; see proxy-process.log")
            if time.time() >= deadline:
                raise TimeoutError("Proxy readiness deadline exceeded")
            try:
                self.identity = json.loads(ready.read_text(encoding="utf-8"))
                break
            except (FileNotFoundError, PermissionError):
                # An atomic publish can still be briefly sharing-locked on
                # Windows. Retry only reads, within the same startup deadline.
                time.sleep(.02)
        expected = {"run_id": ctx.run_id, "pid": self.process.pid, "parent_pid": self.owner_pid,
                    "nonce": self.nonce, "version": PROXY_VERSION, "implementation_sha256": implementation_digest(),
                    "upstream_origin": self.target["origin"], "host": "127.0.0.1"}
        if any(self.identity.get(key) != value for key, value in expected.items()) or type(self.identity.get("port")) is not int:
            raise RuntimeError("Proxy process identity mismatch")
        self.events.record("proxy_verified", identity=self.identity)
        return self

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.identity['port']}"

    def close(self):
        if self.closed:
            return
        try:
            if self.process is not None:
                if self.owner_pid != os.getpid():
                    raise RuntimeError("Refusing to clean an unowned proxy")
                # The graceful stop is local to this unique evidence directory.
                (self.context.evidence_dir / "proxy-stop").touch(exist_ok=False)
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
                save_json(self.context.evidence_dir / "proxy-cleanup.json", {
                    "run_id": self.context.run_id, "nonce": self.nonce, "pid": self.process.pid,
                    "owner_pid": self.owner_pid, "exited": self.process.poll() is not None, "returncode": self.process.returncode})
                self.events.record("proxy_process_stopped", pid=self.process.pid, nonce=self.nonce)
        finally:
            if self.log:
                self.log.close()
            if self.process is None or self.process.poll() is not None:
                self.closed = True

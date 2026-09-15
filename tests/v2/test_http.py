"""Actual TCP and child-process contracts; no ASGI/TestClient stand-in for HTTP."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from agentcheck_biz.adapters.contracts import Observation, PluginConfigurationError, RunContext
from agentcheck_biz.adapters.http_transport import HttpTimeouts, HttpTransport
from agentcheck_biz.adapters.registry import PluginRegistry
from agentcheck_biz.adapters.ticket_http import ticket_http_plugin
from agentcheck_biz.checks import load_json, observe_database
from agentcheck_biz.comparison import compare_runs
from agentcheck_biz.events import EventLog
from agentcheck_biz.lifecycle import run_business_case
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.runner import run_ticket_case
from agentcheck_biz.verifiers.ticket_local import recheck_ticket_run


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def case(self, name="T01"):
        return load_json(REPO_ROOT / f"cases/tickets/full/{name}.json")

    def run_case(self, name="T01", version="fixed", registry=None):
        return run_business_case(self.root / "runs", plugin_id="ticket-http", case=self.case(name),
                                 options={"app_version": version}, registry=registry)

    def prepared(self, name="T01"):
        plugin = ticket_http_plugin(self.case(name), {})
        run_id = "http-test-" + uuid4().hex
        directory = self.root / run_id
        directory.mkdir()
        context = RunContext(run_id, plugin.operation_id, "attempt-one", time.time() + 30, directory)
        events = EventLog(directory / "events.jsonl", run_id)
        self.addCleanup(plugin.environment.cleanup, context, events)
        plugin.environment.prepare(context, events)
        return plugin, context, events

    def assert_stopped(self, directory):
        cleanup = load_json(directory / "http-cleanup.json")
        self.assertTrue(cleanup["exited"])
        identity = load_json(directory / "http-ready.json")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", identity["port"]), timeout=.2)

    def test_three_fixed_contracts_cross_process_and_readonly_recheck(self):
        identities = []
        for name, count in (("T01", 1), ("T02", 1), ("T09", 4)):
            with self.subTest(case=name):
                result = self.run_case(name)
                self.assertEqual(result["result"]["status"], "PASS", result["result"])
                run, directory = result["run"], Path(result["run_dir"])
                self.assertEqual(run["http_business_request_attempts"], count)
                self.assertEqual(run["http_request_attempts"], count + 2)
                self.assertNotEqual(run["http_service"]["pid"], os.getpid())
                self.assertEqual(run["runner_pid"], os.getpid())
                self.assertFalse(run["model_called"])
                before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
                self.assertEqual(recheck_ticket_run(directory)["status"], "PASS")
                self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()})
                self.assert_stopped(directory)
                identities.append(run["http_service"]["instance_id"])
        self.assertEqual(len(set(identities)), 3)

    def test_unsafe_replay_is_a_business_failure(self):
        result = self.run_case("T02", "unsafe")
        self.assertEqual(result["result"]["status"], "FAIL", result["result"])
        self.assertEqual(result["run"]["execution_status"], "completed")
        count = next(row for row in result["result"]["checks"] if row["check_id"] == "ticket_count_for_operation")
        self.assertEqual(count["actual"], 2)
        self.assert_stopped(Path(result["run_dir"]))

    def test_actual_repeated_posts_return_same_persisted_record(self):
        plugin, context, _ = self.prepared()
        env = plugin.environment
        token = env.tokens["tenant-A"]
        results = [env.transport.request("POST", "/tickets", token=token, request_id=f"repeat-{n}",
                   operation_id=plugin.operation_id, body=plugin.case["request"]) for n in range(2)]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], 200)
        rows = observe_database(context.evidence_dir / "business.sqlite")["tickets"]
        self.assertEqual([row for row in rows if row["tenant_id"] == "tenant-A"], [results[0][1]])
        service = (context.evidence_dir / "http-service-events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(service.count('"event": "ticket_created"'), 1)
        self.assertEqual(service.count('"event": "ticket_replayed"'), 1)

    def test_tenant_override_unauthorized_and_foreign_run_cannot_write(self):
        plugin, context, _ = self.prepared("T09")
        other, _, _ = self.prepared()
        env = plugin.environment
        self.assertNotEqual(env.identity["port"], other.environment.identity["port"])
        self.assertNotEqual(env.tokens["tenant-A"], other.environment.tokens["tenant-A"])
        headers = {"Authorization": env.tokens["tenant-A"], "X-Run-Id": context.run_id,
                   "X-Request-Id": "attack", "X-Attempt-Id": context.attempt_id,
                   "X-Operation-Id": plugin.operation_id}
        initial = observe_database(context.evidence_dir / "business.sqlite")
        with httpx.Client(base_url=env.transport.origin, trust_env=False, timeout=2) as client:
            for code, changes, body in (
                (403, {"Authorization": "invalid"}, plugin.case["request"]),
                (403, {"Authorization": other.environment.tokens["tenant-A"]}, plugin.case["request"]),
                (403, {"X-Run-Id": "foreign-run"}, plugin.case["request"]),
                (422, {}, {**plugin.case["request"], "tenant_id": "tenant-C"}),
                (422, {}, {**plugin.case["request"], "operation_id": "replace-operation"}),
            ):
                self.assertEqual(client.post("/tickets", headers={**headers, **changes}, json=body).status_code, code)
            self.assertEqual(client.get("/tickets?tenant_id=tenant-C", headers=headers).status_code, 422)
            self.assertEqual(observe_database(context.evidence_dir / "business.sqlite"), initial)
            response = client.post("/tickets", headers={**headers, "X-Tenant-Id": "tenant-C"}, json=plugin.case["request"])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["tenant_id"], "tenant-A")
            # A mismatched payload under the same bound operation is a real HTTP 409.
            self.assertEqual(client.post("/tickets", headers=headers,
                             json={**plugin.case["request"], "description": "different"}).status_code, 409)
            self.assertEqual(client.get("/tickets", headers={**headers, "Authorization": env.tokens["tenant-C"]}).json(), [])
        # Credentials remain memory-only even after rejected requests are logged.
        for path in context.evidence_dir.iterdir():
            if path.is_file():
                content = path.read_bytes()
                for token in [*env.tokens.values(), *other.environment.tokens.values()]:
                    self.assertNotIn(token.encode(), content)

    def test_bad_health_version_stops_before_any_business_write_and_cleans(self):
        original = HttpTransport.request

        def mismatched(transport, method, path, **kwargs):
            status, data = original(transport, method, path, **kwargs)
            if path == "/version":
                data = {**data, "app_version": "other-version"}
            return status, data

        with patch.object(HttpTransport, "request", mismatched):
            result = self.run_case()
        self.assertEqual(result["result"]["status"], "ERROR")
        self.assertEqual(result["run"]["http_business_request_attempts"], 0)
        self.assert_stopped(Path(result["run_dir"]))

    def test_missing_final_observation_does_not_accept_http_success(self):
        def factory(case, options):
            plugin = ticket_http_plugin(case, options)
            original = plugin.observer.observe
            count = 0

            def observe(context):
                nonlocal count
                count += 1
                return original(context) if count == 1 else Observation.failure(context, "sqlite-readonly:business.sqlite", OSError("read denied"))

            plugin.observer.observe = observe
            return plugin

        registry = PluginRegistry()
        registry.register("ticket-http", factory)
        result = self.run_case(registry=registry)
        self.assertEqual(result["result"]["status"], "ERROR")
        self.assertEqual(result["run"]["client_result"]["status"], "completed")
        self.assert_stopped(Path(result["run_dir"]))

    def test_saved_http_evidence_must_be_present_and_consistent(self):
        result = self.run_case()
        directory = Path(result["run_dir"])
        self.assertEqual(result["result"]["status"], "PASS")
        for filename in ("http-probes.json", "http-observer.jsonl", "http-service-events.jsonl", "http-cleanup.json"):
            with self.subTest(file=filename):
                path = directory / filename
                original = path.read_bytes()
                path.write_text("{}" if path.suffix == ".json" else "", encoding="utf-8")
                self.assertEqual(recheck_ticket_run(directory)["status"], "ERROR")
                path.write_bytes(original)
        path = directory / "run.json"
        original = path.read_bytes()
        run = load_json(path)
        run["http_request_attempts"] += 1
        path.write_text(json.dumps(run), encoding="utf-8")
        self.assertEqual(recheck_ticket_run(directory)["status"], "ERROR")
        path.write_bytes(original)
        (directory / "business.sqlite").write_bytes(b"invalid sqlite")
        self.assertEqual(recheck_ticket_run(directory)["status"], "ERROR")

    def test_incompatible_fault_model_and_timeout_options_reject_before_allocation(self):
        for case, options in ((self.case("T03"), {}), (self.case(), {"agent": "llm"}),
                              (self.case(), {"timeouts": {"read": 0}}),
                              (self.case(), {"timeouts": {"total": float("nan")}}),
                              (self.case(), {"origin": "http://remote.invalid"})):
            with self.assertRaises(PluginConfigurationError):
                run_business_case(self.root / "absent", plugin_id="ticket-http", case=case, options=options)
        self.assertFalse((self.root / "absent").exists())

    def test_http_and_local_are_not_reported_as_a_controlled_repeat(self):
        remote = self.run_case()
        local = run_ticket_case(self.root / "runs", app_version="fixed", inject_fault=False, case=self.case())
        comparison = compare_runs(Path(remote["run_dir"]), Path(local["run_dir"]))
        self.assertFalse(comparison["controlled"])
        self.assertEqual(comparison["comparison_type"], "multiple_changes")

    def test_http_service_source_is_in_implementation_digest(self):
        service = self.root / "examples/ticket_http/server.py"
        service.parent.mkdir(parents=True)
        service.write_text("version = 1\n", encoding="utf-8")
        with patch("agentcheck_biz.provenance.REPO_ROOT", self.root):
            before = implementation_digest()
            service.write_text("version = 2\n", encoding="utf-8")
            self.assertNotEqual(before, implementation_digest())

    def test_expired_deadline_sends_no_request(self):
        run_id = "expired"
        directory = self.root / run_id
        directory.mkdir()
        context = RunContext(run_id, "op", "attempt", time.time() - 1, directory)
        events = EventLog(directory / "events.jsonl", run_id)
        transport = HttpTransport("http://127.0.0.1:1", context, events, HttpTimeouts())
        with self.assertRaises(TimeoutError):
            transport.request("POST", "/tickets", token="test", request_id="one")
        self.assertEqual(transport.attempts, 0)

    def test_read_and_total_timeout_cancel_real_network_without_retry(self):
        for mode in ("no_response", "slow_body", "run_deadline"):
            with self.subTest(mode=mode):
                stop = threading.Event()
                received = []

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_POST(self):
                        self.rfile.read(int(self.headers.get("Content-Length", "0")))
                        received.append(self.path)
                        if mode == "no_response":
                            stop.wait(2)
                            return
                        self.send_response(200)
                        self.send_header("Content-Length", "200")
                        self.end_headers()
                        try:
                            while not stop.wait(.025):
                                self.wfile.write(b" ")
                                self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                            pass

                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
                worker.start()
                directory = self.root / mode
                directory.mkdir()
                context = RunContext(mode, "op", "attempt", time.time() + (.4 if mode == "run_deadline" else 5), directory)
                events = EventLog(directory / "events.jsonl", mode)
                limits = HttpTimeouts(connect=.3, read=.15, total=.4 if mode == "slow_body" else 2)
                transport = HttpTransport(f"http://127.0.0.1:{server.server_port}", context, events, limits)
                try:
                    started = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        transport.request("POST", "/tickets", token="test", request_id="one", body={})
                    self.assertLess(time.monotonic() - started, 1.5)
                    self.assertEqual(received, ["/tickets"])
                    self.assertEqual(transport.business_attempts, 1)
                    failure = events.items[-1]
                    self.assertEqual(failure["event"], "http_request_failed")
                    self.assertEqual(failure["error_type"], "ReadTimeout" if mode == "no_response" else "TimeoutError")
                finally:
                    stop.set()
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

"""D20: genuine SDK sessions, both business oracles and contract negatives."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from agentcheck_biz.adapters.contracts import Observation, PluginConfigurationError
from agentcheck_biz.adapters.gitea_mcp import gitea_mcp_plugin
from agentcheck_biz.adapters.mcp_execution import McpExecution
from agentcheck_biz.adapters.mcp_transport import McpConnection, McpToolError
from agentcheck_biz.adapters.registry import PluginRegistry
from agentcheck_biz.adapters.ticket_mcp import ticket_mcp_plugin
from agentcheck_biz.checks import load_json
from agentcheck_biz.events import EventLog
from agentcheck_biz.lifecycle import run_business_case
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from agentcheck_biz.verifiers.ticket_local import recheck_ticket_run
from agentcheck_biz.verifiers.gitea import recheck_gitea_run
from examples.business_mcp import SDK_VERSION, PROTOCOL_VERSION
from examples.gitea_target.demo import target_environment
from examples.gitea_target.runtime import DEFAULT_BINARY, GiteaRuntime


def ticket(name="T01"):
    return load_json(REPO_ROOT / f"cases/tickets/full/{name}.json")


def issue(name="G01"):
    return load_json(REPO_ROOT / f"cases/gitea/{name}.json")


def log(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TicketMcpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.normal = run_business_case(cls.root / "runs", plugin_id="ticket-mcp", case=ticket())
        cls.replay = run_business_case(cls.root / "runs", plugin_id="ticket-mcp", case=ticket("T02"))
        cls.isolation = run_business_case(cls.root / "runs", plugin_id="ticket-mcp", case=ticket("T09"))

    def test_normal_handshake_http_and_independent_sqlite_oracle(self):
        result = self.normal
        self.assertEqual(result["result"]["status"], "PASS", result["result"])
        directory = Path(result["run_dir"])
        self.assertEqual(recheck_ticket_run(directory)["status"], "PASS")
        self.assertEqual(result["run"]["mcp_sdk_version"], SDK_VERSION)
        self.assertEqual(result["run"]["mcp_protocol_version"], PROTOCOL_VERSION)
        self.assertNotEqual(load_json(directory / "mcp-1-identity.json")["pid"], result["run"]["runner_pid"])
        self.assertTrue(load_json(directory / "mcp-1-cleanup.json")["exited"])
        self.assertTrue(load_json(directory / "http-cleanup.json")["exited"])

    def test_replay_preserves_operation_and_allocates_new_attempt_each_time(self):
        self.assertEqual(self.replay["result"]["status"], "PASS", self.replay["result"])
        directory = Path(self.replay["run_dir"])
        calls = [row for row in log(directory / "events.jsonl") if row["event"] == "tool_called"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({r["operation_id"] for r in calls}), 1)
        self.assertEqual(len({r["attempt_id"] for r in calls}), 2)
        received = [row for row in log(directory / "http-service-events.jsonl") if row["event"] == "request_received" and row["path"] == "/tickets"]
        self.assertEqual([r["attempt_id"] for r in calls], [r["attempt_id"] for r in received])

    def test_two_tenants_use_separate_bound_sessions(self):
        self.assertEqual(self.isolation["result"]["status"], "PASS", self.isolation["result"])
        directory = Path(self.isolation["run_dir"])
        sessions = [load_json(directory / (name + "-session.json")) for name in self.isolation["run"]["mcp_connections"]]
        self.assertEqual(len(sessions), 2)
        self.assertEqual(len({s["identity"]["pid"] for s in sessions}), 2)
        self.assertEqual(len({s["scope"]["tenant_id"] for s in sessions}), 2)

    def test_business_only_catalog_excludes_platform_identity_and_credentials(self):
        session = load_json(Path(self.normal["run_dir"]) / "mcp-1-session.json")
        properties = {tool["name"]: set(tool["inputSchema"]["properties"]) for tool in session["tools"]["tools"]}
        self.assertEqual(properties, {"create_ticket": {"customer_id", "device_id", "description"}, "query_tickets": set()})
        self.assertTrue(all(tool["inputSchema"]["additionalProperties"] is False for tool in session["tools"]["tools"]))

    def test_corrupt_or_missing_protocol_evidence_never_passes(self):
        def missing(directory):
            (directory / "mcp-1-wire.jsonl").unlink()

        def protocol(directory):
            run = load_json(directory / "run.json")
            run["mcp_protocol_version"] = "untested"
            save_json(directory / "run.json", run)

        def identity(directory):
            value = load_json(directory / "mcp-1-cleanup.json")
            value["exited"] = False
            save_json(directory / "mcp-1-cleanup.json", value)

        def context(directory):
            path = directory / "mcp-1-wire.jsonl"
            rows = log(path)
            for row in rows:
                if row["message"].get("method") == "tools/call":
                    row["message"]["params"]["_meta"]["agentcheck"]["operation_id"] = "other"
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

        for mutate in (missing, protocol, identity, context):
            with self.subTest(mutation=mutate.__name__):
                directory = self.root / uuid4().hex / Path(self.normal["run_dir"]).name
                shutil.copytree(self.normal["run_dir"], directory)
                mutate(directory)
                self.assertEqual(recheck_ticket_run(directory)["status"], "ERROR")

    def test_observer_failure_remains_error_after_successful_mcp_write(self):
        def factory(case, options):
            plugin = ticket_mcp_plugin(case, options)
            original = plugin.observer.observe
            counter = 0

            def observe(context):
                nonlocal counter
                counter += 1
                return original(context) if counter == 1 else Observation.failure(context, "sqlite-readonly:business.sqlite", PermissionError("read-only observer unavailable"))

            plugin.observer.observe = observe
            return plugin

        registry = PluginRegistry()
        registry.register("ticket-mcp", factory)
        result = run_business_case(self.root / "runs", plugin_id="ticket-mcp", case=ticket(), registry=registry)
        self.assertEqual(result["result"]["status"], "ERROR")
        self.assertEqual(result["run"]["tool_calls"], 1)
        self.assertFalse(load_json(Path(result["run_dir"]) / "observation-final.json")["complete"])

    def test_unsupported_faults_rejected_before_allocation(self):
        destination = self.root / "not-allocated"
        with self.assertRaises(PluginConfigurationError):
            run_business_case(destination, plugin_id="ticket-mcp", case=ticket("T03"))
        self.assertFalse(destination.exists())

    def test_server_rejects_unknown_invalid_reused_context_and_client_validates(self):
        from agentcheck_biz.adapters.contracts import RunContext
        run_id = "mcp-negative-" + uuid4().hex
        directory = self.root / run_id
        directory.mkdir()
        context = RunContext(run_id, "repair-001", "attempt-" + uuid4().hex, time.time() + 25, directory)
        events = EventLog(directory / "events.jsonl", run_id)
        # Rejected calls must not need or reach an HTTP server.
        binding = {"backend": "ticket-http", "origin": "http://127.0.0.1:1", "execution_token": "test-only-sentinel",
                   "scope": {"operation_id": context.operation_id, "tenant_id": "tenant-A"}, "timeouts": {}}
        connection = McpConnection(context, events, binding, 1)

        async def exercise():
            async with connection.connect():
                for index, (tool, args, validate) in enumerate((
                    ("unknown_tool", {}, True), ("create_ticket", {"customer_id": 2}, True),
                    ("unknown_tool", {}, False), ("create_ticket", {"customer_id": 2}, False),
                    ("create_ticket", {**ticket()["request"], "tenant_id": "other"}, False)), 1):
                    with self.assertRaises(McpToolError):
                        await connection.call(tool, args, call_id=f"call-{index}",
                            attempt_id=context.attempt_id + f"/call-{index}-" + uuid4().hex, validate=validate)
                # A repeated call context is invalid even when business content changes.
                with self.assertRaisesRegex(McpToolError, "reused platform call context"):
                    await connection.call("query_tickets", {}, call_id="call-3",
                        attempt_id=context.attempt_id + "/call-3-" + uuid4().hex)

        asyncio.run(exercise())
        self.assertEqual((directory / "mcp-1-http.jsonl").read_text(encoding="utf-8"), "")
        self.assertTrue(load_json(directory / "mcp-1-cleanup.json")["exited"])
        sent = [row for row in log(directory / "mcp-1-wire.jsonl") if row["event"] == "sent" and row["message"].get("method") == "tools/call"]
        self.assertEqual(len(sent), 4)  # Two client rejections did not cross the pipe.

    def test_expired_deadline_starts_no_process(self):
        from agentcheck_biz.adapters.contracts import RunContext
        run_id = "mcp-expired-" + uuid4().hex
        directory = self.root / run_id
        directory.mkdir()
        context = RunContext(run_id, "op", "attempt", time.time() - 1, directory)
        connection = McpConnection(context, EventLog(directory / "events.jsonl", run_id), {}, 1)

        async def exercise():
            async with connection.connect():
                self.fail("Expired context entered the session")

        with self.assertRaises(TimeoutError):
            asyncio.run(exercise())
        self.assertIsNone(connection.process)

    def test_inflight_deadline_exits_owned_process(self):
        from agentcheck_biz.adapters.contracts import RunContext
        release = Event()

        class SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                release.wait(10)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"[]")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        run_id = "mcp-timeout-" + uuid4().hex
        directory = self.root / run_id
        directory.mkdir()
        context = RunContext(run_id, "op", "attempt-" + uuid4().hex, time.time() + 4, directory)
        binding = {"backend": "ticket-http", "origin": f"http://127.0.0.1:{server.server_port}",
                   "execution_token": "timeout-test", "scope": {"operation_id": "op", "tenant_id": "tenant-A"},
                   "timeouts": {"connect": 1, "read": 10, "total": 10}}
        connection = McpConnection(context, EventLog(directory / "events.jsonl", run_id), binding, 1)

        async def exercise():
            async with connection.connect():
                await connection.call("query_tickets", {}, call_id="call-1", attempt_id=context.attempt_id + "/call-1-" + uuid4().hex)

        start = time.monotonic()
        try:
            with self.assertRaises(Exception):
                asyncio.run(exercise())
            self.assertLess(time.monotonic() - start, 10)
            self.assertIsNotNone(connection.process.returncode)
            self.assertTrue(load_json(directory / "mcp-1-cleanup.json")["exited"])
            self.assertTrue(any(row["event"] == "http_request_started" for row in log(directory / "mcp-1-http.jsonl")))
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_generic_executor_and_lifecycle_contain_no_object_branches(self):
        for path in ("agentcheck_biz/lifecycle.py", "agentcheck_biz/adapters/mcp_execution.py", "agentcheck_biz/adapters/mcp_transport.py"):
            source = (REPO_ROOT / path).read_text(encoding="utf-8").lower()
            self.assertNotIn("gitea", source)
            self.assertNotIn("ticket", source)


@unittest.skipUnless(DEFAULT_BINARY.is_file(), "Pinned official Gitea binary required")
class GiteaMcpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.runtime = GiteaRuntime(cls.root / "instances").start()
        cls.addClassCleanup(cls.runtime.close)
        cls.env = target_environment(cls.runtime)
        cls.env.__enter__()
        cls.addClassCleanup(cls.env.__exit__, None, None, None)
        cls.runs = {name: run_business_case(cls.root / "runs", plugin_id="gitea-mcp", case=issue(name)) for name in ("G01", "G02", "G03")}

    def test_create_uses_actual_api_and_offline_independent_oracle(self):
        item = self.runs["G01"]
        self.assertEqual(item["result"]["status"], "PASS", item["result"])
        with patch("httpx.AsyncClient.request", side_effect=AssertionError("Offline recheck must not access network")):
            self.assertEqual(recheck_gitea_run(item["run_dir"])["status"], "PASS")
        self.assertEqual(type(gitea_mcp_plugin(issue(), {}).execution), McpExecution)
        self.assertEqual(type(ticket_mcp_plugin(ticket(), {}).execution), McpExecution)

    def test_duplicate_is_business_fail_with_fresh_attempts_and_same_operation(self):
        item = self.runs["G02"]
        self.assertEqual(item["result"]["status"], "FAIL", item["result"])
        calls = [r for r in log(Path(item["run_dir"]) / "events.jsonl") if r["event"] == "tool_called"]
        self.assertEqual(len({r["attempt_id"] for r in calls}), 2)
        self.assertEqual(len({r["operation_id"] for r in calls}), 1)
        self.assertEqual(recheck_gitea_run(item["run_dir"])["status"], "FAIL")

    def test_close_preserves_unrelated_and_observes_every_page(self):
        item = self.runs["G03"]
        self.assertEqual(item["result"]["status"], "PASS", item["result"])
        state = load_json(Path(item["run_dir"]) / "observation-final.json")["data"]
        self.assertEqual(state["pagination"]["total_count"], 6)
        self.assertEqual(len(state["pagination"]["pages"]), 3)
        repos = [load_json(Path(run["run_dir"]) / "gitea-target.json")["repository"] for run in self.runs.values()]
        self.assertEqual(len({r["full_name"] for r in repos}), 3)
        self.assertTrue(all(r["private"] for r in repos))

    def test_actual_missing_repository_read_permission_never_becomes_empty_success(self):
        def factory(case, options):
            plugin = gitea_mcp_plugin(case, options)
            original = plugin.execution.behavior.run

            async def execute(call, events):
                result = await original(call, events)
                # Actual execution token has write:issue, but no read:repository.
                plugin.environment.observer_client._token = plugin.environment.settings.execution_token
                return result

            plugin.execution.behavior.run = execute
            return plugin

        registry = PluginRegistry()
        registry.register("gitea-mcp", factory)
        item = run_business_case(self.root / "runs", plugin_id="gitea-mcp", case=issue(), registry=registry)
        self.assertEqual(item["result"]["status"], "ERROR", item["result"])
        observation = load_json(Path(item["run_dir"]) / "observation-final.json")
        self.assertFalse(observation["complete"])
        self.assertIsNone(observation["data"])
        responses = [load_json(path) for path in (Path(item["run_dir"]) / "gitea-api").glob("observer-*.json")]
        self.assertIn(403, [r["status_code"] for r in responses])

    def test_no_scoped_credentials_in_tool_schema_or_saved_evidence(self):
        for item in self.runs.values():
            directory = Path(item["run_dir"])
            session = load_json(directory / "mcp-1-session.json")
            self.assertEqual({t["name"]: set(t["inputSchema"]["properties"]) for t in session["tools"]["tools"]},
                             {"create_issue": {"title", "body"}, "close_issue": {"number"}})
            tokens = [value for key, value in self.runtime.environment().items() if key.endswith("_TOKEN")]
            for path in directory.rglob("*"):
                if path.is_file():
                    raw = path.read_bytes()
                    self.assertTrue(all(token.encode() not in raw for token in tokens), path)

    def test_missing_execution_api_response_prevents_pass(self):
        source = Path(self.runs["G01"]["run_dir"])
        directory = self.root / "tampered" / source.name
        shutil.copytree(source, directory)
        (directory / "gitea-api/execution-1.json").unlink()
        self.assertEqual(recheck_gitea_run(directory)["status"], "ERROR")

"""D21 actual loopback connections, independent targets and fault coverage."""

from copy import deepcopy
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch, AsyncMock
from uuid import uuid4

import httpx

from agentcheck_biz.adapters.contracts import PluginConfigurationError, RunContext
from agentcheck_biz.checks import load_json
from agentcheck_biz.events import EventLog
from agentcheck_biz.fault_proxy.rules import loopback, validate_rule
from agentcheck_biz.fault_proxy.runtime import ProxyRuntime
from agentcheck_biz.fault_proxy.verify import assess_proxy_evidence
from agentcheck_biz.lifecycle import run_business_case
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from agentcheck_biz.verifiers.ticket_local import recheck_ticket_run
from agentcheck_biz.verifiers.gitea import recheck_gitea_run
from examples.business_mcp.proxy_demo import run_proxy_case
from examples.gitea_target.demo import target_environment
from examples.gitea_target.runtime import DEFAULT_BINARY, GiteaRuntime


def profile(number):
    return load_json(next((REPO_ROOT / "cases/proxy").glob(f"P{number:02d}_*.json")))


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class ProxySocketTests(unittest.TestCase):
    def test_timer_waking_early_does_not_end_requested_delay(self):
        from agentcheck_biz.fault_proxy.server import wait_delay
        with patch("agentcheck_biz.fault_proxy.server.time.monotonic_ns", side_effect=[0, 0, 50_000_000, 120_000_000]), \
                patch("agentcheck_biz.fault_proxy.server.asyncio.sleep", new_callable=AsyncMock) as sleep:
            asyncio.run(wait_delay(120))
        self.assertEqual(sleep.await_count, 2)

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.receipts = []

        class Echo(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                cls.receipts.append({"run_id": self.headers.get("X-Run-Id"), "body": body, "headers": dict(self.headers)})
                self.send_response(201)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("X-Echo", "preserved")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def proxy(self, rule=None):
        run_id = "proxy-test-" + uuid4().hex
        directory = self.root / run_id
        directory.mkdir()
        ctx = RunContext(run_id, "operation", "attempt-root", time.time() + 45, directory)
        target = {"origin": f"http://127.0.0.1:{self.server.server_port}", "routes": {
            "create_ticket": {"method": "POST", "path_pattern": "/echo"},
            "query_tickets": {"method": "GET", "path_pattern": "/echo"}}}
        runtime = ProxyRuntime(ctx, EventLog(directory / "events.jsonl", run_id), target, rule)
        self.addCleanup(runtime.close)
        return runtime.start()

    def request(self, proxy, number=1, *, tool="create_ticket", body=b'{"text":"hello"}', read=2, overrides=None, path="/echo"):
        headers = {"Authorization": "Bearer test-execution-sentinel", "Content-Type": "application/json", "X-Run-Id": proxy.context.run_id,
            "X-Operation-Id": "operation", "X-Request-Id": f"call-{number}", "X-Call-Id": f"call-{number}",
            "X-Attempt-Id": uuid4().hex, "X-AgentCheck-Tool": tool, **(overrides or {})}
        with httpx.Client(transport=httpx.HTTPTransport(retries=0), trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(read, connect=1)) as client:
            return client.request("GET" if tool == "query_tickets" else "POST", proxy.origin + path, headers=headers, content=body)

    def trace(self, proxy):
        return rows(proxy.context.evidence_dir / "proxy-events.jsonl")

    def receipts_for(self, proxy):
        return [r for r in self.receipts if r["run_id"] == proxy.context.run_id]

    def test_transparent_bytes_headers_status_and_no_credentials_in_logs(self):
        proxy = self.proxy()
        body = ' { "文字" : "原始空格与转义\\n", "a": [1,2] } '.encode()
        result = self.request(proxy, body=body)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.content, body)
        self.assertEqual(result.headers["x-echo"], "preserved")
        receipt = self.receipts_for(proxy)[0]
        self.assertEqual(receipt["body"], body)
        self.assertEqual(receipt["headers"]["authorization"], "Bearer test-execution-sentinel")
        proxy.close()
        trace = self.trace(proxy)
        self.assertEqual([r["event"] for r in trace].count("upstream_connecting"), 1)
        self.assertNotIn("test-execution-sentinel", (proxy.context.evidence_dir / "proxy-events.jsonl").read_text(encoding="utf-8"))

    def test_readiness_sharing_lock_retries_read_without_restarting_process(self):
        original, failures = Path.read_text, []

        def read(path, *args, **kwargs):
            if path.name == "proxy-ready.json" and path.exists() and len(failures) < 2:
                failures.append(path)
                raise PermissionError("temporary Windows sharing lock")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read):
            proxy = self.proxy()
        self.assertEqual(len(failures), 2)
        self.assertEqual(self.request(proxy).status_code, 201)
        parent = rows(proxy.context.evidence_dir / "events.jsonl")
        self.assertEqual(sum(r["event"] == "proxy_process_created" for r in parent), 1)

    def test_reject_before_upstream_is_real_network_error_and_no_write(self):
        proxy = self.proxy(profile(1)["rule"])
        with self.assertRaises(httpx.RemoteProtocolError):
            self.request(proxy)
        self.assertFalse(self.receipts_for(proxy))
        self.assertFalse(any(r["event"] == "upstream_connecting" for r in self.trace(proxy)))

    def test_drop_reads_complete_upstream_then_aborts_without_http_error_body(self):
        proxy = self.proxy(profile(3)["rule"])
        with self.assertRaises(httpx.RemoteProtocolError):
            self.request(proxy)
        self.assertEqual(len(self.receipts_for(proxy)), 1)
        trace = self.trace(proxy)
        order = [r["event"] for r in trace]
        self.assertLess(order.index("upstream_response"), order.index("downstream_aborted"))
        self.assertNotIn("downstream_sent", order)

    def test_delay_shorter_than_timeout_preserves_response_and_waits(self):
        rule = profile(2)["rule"]
        rule["delay_ms"] = 120
        proxy = self.proxy(rule)
        result = self.request(proxy)
        self.assertEqual(result.status_code, 201)
        trace = {r["event"]: r for r in self.trace(proxy)}
        self.assertGreaterEqual(trace["delay_finished"]["monotonic_ns"] - trace["delay_started"]["monotonic_ns"], 120_000_000)

    def test_delay_longer_than_timeout_causes_real_read_timeout(self):
        proxy = self.proxy(profile(2)["rule"])
        with self.assertRaises(httpx.ReadTimeout):
            self.request(proxy, read=.2)
        self.assertEqual(len(self.receipts_for(proxy)), 1)
        self.assertTrue(any(r["event"] == "delay_started" for r in self.trace(proxy)))

    def test_trigger_limit_prevents_repeated_fault_and_disables_hidden_retries(self):
        rule = profile(3)["rule"]
        rule["request_numbers"] = [1, 2]
        proxy = self.proxy(rule)
        with self.assertRaises(httpx.RemoteProtocolError):
            self.request(proxy, 1)
        self.assertEqual(self.request(proxy, 2).status_code, 201)
        proxy.close()
        summary = load_json(proxy.context.evidence_dir / "proxy-summary.json")
        self.assertEqual(summary["triggers"], 1)
        self.assertEqual(summary["connections"], 2)
        self.assertEqual(summary["upstream_attempts"], 2)

    def test_request_number_is_scoped_to_tool(self):
        rule = profile(3)["rule"]
        rule["request_numbers"] = [2]
        proxy = self.proxy(rule)
        self.assertEqual(self.request(proxy, 1, tool="query_tickets").status_code, 201)
        self.assertEqual(self.request(proxy, 2).status_code, 201)
        with self.assertRaises(httpx.RemoteProtocolError):
            self.request(proxy, 3)
        hit = next(r for r in self.trace(proxy) if r["event"] == "fault_triggered")
        self.assertEqual((hit["call_id"], hit["request_number"], hit["tool"]), ("call-3", 2, "create_ticket"))

    def test_other_run_or_outside_route_cannot_be_forwarded(self):
        proxy = self.proxy(profile(3)["rule"])
        with self.assertRaises(httpx.RemoteProtocolError):
            self.request(proxy, overrides={"X-Run-Id": "different-run"})
        with self.assertRaises(httpx.RemoteProtocolError):
            self.request(proxy, path="/api/admin")
        self.assertFalse(self.receipts_for(proxy))
        self.assertFalse(any(r["event"] == "fault_triggered" for r in self.trace(proxy)))

    def test_other_proxy_run_and_process_survive_owned_cleanup(self):
        first, second = self.proxy(profile(3)["rule"]), self.proxy()
        with self.assertRaises(httpx.RemoteProtocolError):
            self.request(first)
        first.close()
        first.close()
        self.assertIsNone(second.process.poll())
        self.assertEqual(self.request(second).status_code, 201)
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", first.identity["port"])), 0)

    def test_rule_for_another_run_is_uncovered(self):
        rule = profile(3)["rule"]
        rule["run_id"] = "different-run"
        proxy = self.proxy(rule)
        self.assertEqual(self.request(proxy).status_code, 201)
        proxy.close()
        self.assertEqual(load_json(proxy.context.evidence_dir / "proxy-summary.json")["coverage"], "not_covered")

    def test_configuration_rejects_external_target_wrong_version_and_arbitrary_fields(self):
        for origin in ("https://example.org", "http://localhost:80", "http://127.0.0.1:80/path", "http://127.0.0.1:80?url=other"):
            with self.subTest(origin=origin), self.assertRaises(PluginConfigurationError):
                loopback(origin)
        for key, value in (("schema_version", 2), ("max_triggers", 0), ("phase", "commit_confirmed"), ("origin", "http://example.org"), ("delay_ms", -1)):
            rule = profile(3)["rule"]
            rule[key] = value
            with self.subTest(field=key), self.assertRaises(PluginConfigurationError):
                validate_rule(rule)
        directory = self.root / "not-created"
        with self.assertRaises(PluginConfigurationError):
            run_business_case(directory, plugin_id="ticket-mcp", case=load_json(REPO_ROOT / "cases/tickets/full/T01.json"), options={"proxy": {"upstream": "arbitrary"}})
        self.assertFalse(directory.exists())


class TicketProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.runs = {number: run_proxy_case(cls.root / "runs", plugin_id="ticket-mcp",
            case=load_json(REPO_ROOT / "cases/tickets/full/T01.json"), proxy={"rule": profile(number)["rule"]}) for number in (0, 1, 2, 3, 4)}

    def test_all_three_faults_have_network_errors_and_independent_final_sqlite(self):
        for number in (1, 2, 3):
            item = self.runs[number]
            self.assertEqual(item["proxy_contract"]["status"], "PASS", item["proxy_contract"])
            self.assertEqual(item["result"]["status"], "ERROR")
            directory = Path(item["run_dir"])
            state = load_json(directory / "final.json")
            operation = item["run"]["context"]["operation_id"]
            matching = [r for r in state["tickets"] if r["operation_id"] == operation and r["tenant_id"] == "tenant-A"]
            self.assertEqual(len(matching), 0 if number == 1 else 1)
            self.assertEqual(recheck_ticket_run(directory)["status"], "ERROR")

    def test_transparent_path_passes_both_mcp_and_business_oracle(self):
        item = self.runs[0]
        self.assertEqual(item["result"]["status"], "PASS", item["result"])
        self.assertEqual(item["proxy_contract"]["coverage"], "transparent")
        self.assertEqual(recheck_ticket_run(item["run_dir"])["status"], "PASS")

    def test_unreached_rule_never_passes_even_when_business_row_exists(self):
        item = self.runs[4]
        self.assertEqual(item["result"]["status"], "INCONCLUSIVE", item["result"])
        self.assertEqual(item["proxy_contract"]["coverage"], "not_covered")
        self.assertEqual(recheck_ticket_run(item["run_dir"])["status"], "INCONCLUSIVE")

    def test_missing_client_error_and_forged_stage_or_cleanup_prevent_coverage_pass(self):
        def missing_error(directory):
            file = directory / "mcp-1-http.jsonl"
            trace = rows(file)
            for row in trace:
                if row["event"] == "http_request_failed":
                    row["error_type"] = "BusinessRejected"
            file.write_text("\n".join(json.dumps(r) for r in trace) + "\n", encoding="utf-8")

        def wrong_phase(directory):
            file = directory / "proxy-events.jsonl"
            trace = rows(file)
            for row in trace:
                if row["event"] == "fault_triggered":
                    row["phase"] = "before_forward"
            file.write_text("\n".join(json.dumps(r) for r in trace) + "\n", encoding="utf-8")

        def missing_cleanup(directory):
            (directory / "proxy-cleanup.json").unlink()

        for mutate in (missing_error, wrong_phase, missing_cleanup):
            with self.subTest(mutation=mutate.__name__):
                source = Path(self.runs[3]["run_dir"])
                directory = self.root / uuid4().hex / source.name
                shutil.copytree(source, directory)
                mutate(directory)
                self.assertEqual(assess_proxy_evidence(directory)["status"], "ERROR")

    def test_two_tenant_sessions_share_proxy_without_losing_isolation(self):
        item = run_proxy_case(self.root / "runs", plugin_id="ticket-mcp", case=load_json(REPO_ROOT / "cases/tickets/full/T09.json"), proxy={"rule": None})
        self.assertEqual(item["result"]["status"], "PASS", item["result"])
        self.assertEqual(item["proxy_contract"]["status"], "PASS", item["proxy_contract"])
        self.assertEqual(load_json(Path(item["run_dir"]) / "proxy-summary.json")["connections"], 4)


@unittest.skipUnless(DEFAULT_BINARY.is_file(), "Pinned official Gitea binary required")
class GiteaProxyTests(unittest.TestCase):
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
        cls.runs = {}
        for number in (0, 1, 2, 3, 4):
            rule = profile(number)["rule"]
            if rule:
                rule["tool"] = "create_issue"
            cls.runs[number] = run_proxy_case(cls.root / "runs", plugin_id="gitea-mcp", case=load_json(REPO_ROOT / "cases/gitea/G01.json"), proxy={"rule": rule})

    def test_gitea_all_mechanisms_and_uncovered_rule(self):
        for number, item in self.runs.items():
            self.assertEqual(item["proxy_contract"]["status"], "INCONCLUSIVE" if number == 4 else "PASS", item["proxy_contract"])
            self.assertEqual(item["result"]["status"], "PASS" if number == 0 else "INCONCLUSIVE" if number == 4 else "ERROR", item["result"])
            self.assertEqual(recheck_gitea_run(item["run_dir"])["status"], item["result"]["status"])

    def test_observer_reads_direct_api_even_when_execution_response_is_lost(self):
        for number in (1, 2, 3):
            item = self.runs[number]
            directory = Path(item["run_dir"])
            observed = load_json(directory / "observation-final.json")
            self.assertTrue(observed["complete"])
            self.assertEqual(len(observed["data"]["issues"]), 0 if number == 1 else 1)
            requests = [r for r in rows(directory / "gitea-observer.jsonl") if r["event"] == "api_request"]
            self.assertTrue(all(r["origin"] == self.runtime.identity["origin"] for r in requests))

    def test_proxy_evidence_never_contains_scoped_tokens(self):
        tokens = [value.encode() for key, value in self.runtime.environment().items() if key.endswith("_TOKEN")]
        for item in self.runs.values():
            for file in Path(item["run_dir"]).rglob("*"):
                if file.is_file():
                    self.assertTrue(all(token not in file.read_bytes() for token in tokens), file)

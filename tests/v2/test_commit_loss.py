"""Real D22 disconnects plus fail-closed evidence and barrier regressions."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from agentcheck_biz.adapters.contracts import PluginConfigurationError
from agentcheck_biz.adapters.registry import builtin_registry
from agentcheck_biz.checks import load_json
from agentcheck_biz.commit_loss.barrier import Controller, bound, wait_message, read_message
from agentcheck_biz.fault_proxy.rules import validate_rule
from agentcheck_biz.fault_proxy.verify import assess_proxy_evidence
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from agentcheck_biz.verifiers.mcp import verify_mcp_evidence
from agentcheck_biz.verifiers.ticket_local import recheck_ticket_run
from examples.business_mcp.commit_loss_demo import ticket_case, gitea_case
from examples.gitea_target.demo import target_environment
from examples.gitea_target.runtime import DEFAULT_BINARY, DEFAULT_INSTANCE_ROOT, GiteaRuntime


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class BarrierProtocolTests(unittest.TestCase):
    def test_foreign_run_nonce_attempt_rejected(self):
        expected = {"run_id": "r", "nonce": "n", "attempt_id": "a"}
        for key in expected:
            with self.subTest(key=key), self.assertRaises(ValueError):
                bound({**expected, key: "foreign"}, expected)

    def test_missing_confirmation_has_bounded_deadline(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(TimeoutError):
            wait_message(Path(temp) / "missing.json", {"run_id": "r"}, time.time() + .02)

    def test_file_sharing_lock_is_pending_not_confirmation(self):
        with patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertIsNone(read_message(Path("pending.json")))

    def test_v2_cannot_select_delay_or_multiple_requests(self):
        rule = load_json(REPO_ROOT / "cases/commit_loss/ticket.json")
        for change in ({"action": "delay"}, {"request_numbers": [1, 2]}, {"phase": "after_upstream_response"}):
            with self.subTest(change=change), self.assertRaises(PluginConfigurationError):
                validate_rule({**rule, **change})

    def test_bad_comparison_configuration_rejected_before_allocation(self):
        case = load_json(REPO_ROOT / "cases/tickets/full/T01.json")
        for options in ({"commit_loss": False}, {"commit_loss": True, "proxy": {"rule": {"schema_version": 1}}}):
            with self.subTest(options=options), self.assertRaises(PluginConfigurationError):
                builtin_registry().resolve("ticket-mcp", case, options)


class TicketCommitLossTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.a = ticket_case(cls.root, "unsafe", False)
        cls.b = ticket_case(cls.root, "unsafe")
        cls.c = ticket_case(cls.root, "fixed")

    def copy_run(self, item=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = Path((item or self.c)["run_dir"])
        return Path(shutil.copytree(source, Path(temp.name) / source.name))

    def test_a_transparent_one_effect(self):
        self.assertEqual((self.a["result"]["status"], self.a["proxy_contract"]["coverage"], self.a["run"]["tool_calls"]),
                         ("PASS", "transparent", 1))

    def test_b_real_loss_retry_exposes_duplicate(self):
        self.assertEqual(self.b["result"]["status"], "FAIL", self.b["result"])
        self.assertEqual(self.b["proxy_contract"]["coverage"], "covered", self.b["proxy_contract"])
        count = next(c for c in self.b["result"]["checks"] if c["check_id"] == "ticket_count_for_operation")
        self.assertEqual(count["actual"], 2)

    def test_c_real_loss_replay_has_one_effect(self):
        self.assertEqual(self.c["result"]["status"], "PASS", self.c["result"])
        self.assertEqual(self.c["proxy_contract"]["coverage"], "covered", self.c["proxy_contract"])
        service = rows(Path(self.c["run_dir"]) / "http-service-events.jsonl")
        self.assertEqual(sum(r["event"] == "ticket_replayed" for r in service), 1)

    def test_b_c_same_rule_request_budget_distinct_attempts(self):
        b, c = (Path(item["run_dir"]) for item in (self.b, self.c))
        self.assertEqual(load_json(b / "case.json"), load_json(c / "case.json"))
        rules = [load_json(p / "proxy-rule.json") for p in (b, c)]
        self.assertEqual({k: v for k, v in rules[0].items() if k != "run_id"},
                         {k: v for k, v in rules[1].items() if k != "run_id"})
        for item, directory in ((self.b, b), (self.c, c)):
            calls = [r for r in rows(directory / "events.jsonl") if r["event"] == "tool_called"]
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0]["attempt_id"], calls[1]["attempt_id"])
            self.assertEqual(calls[0]["operation_id"], calls[1]["operation_id"])
            self.assertEqual(calls[0]["deadline"], calls[1]["deadline"])

    def test_independent_read_failure_denies_drop_and_is_not_covered(self):
        with patch.object(Controller, "confirm", side_effect=OSError("read-only observation unavailable")):
            item = ticket_case(self.root, "fixed")
        self.assertEqual((item["result"]["status"], item["proxy_contract"]["coverage"]), ("INCONCLUSIVE", "not_covered"))
        self.assertEqual(item["run"]["tool_calls"], 1)
        self.assertFalse(any(r["event"] == "downstream_aborted" for r in rows(Path(item["run_dir"]) / "proxy-events.jsonl")))

    def test_missing_confirmation_file_is_not_covered(self):
        directory = self.copy_run()
        (directory / "commit-loss-decision.json").unlink()
        self.assertEqual(assess_proxy_evidence(directory)["status"], "INCONCLUSIVE")
        self.assertEqual(recheck_ticket_run(directory)["status"], "INCONCLUSIVE")

    def test_foreign_confirmation_identity_is_rejected(self):
        directory = self.copy_run()
        path = directory / "commit-loss-decision.json"
        save_json(path, {**load_json(path), "attempt_id": "foreign"})
        self.assertEqual(assess_proxy_evidence(directory)["status"], "ERROR")

    def test_confirmation_after_abort_is_rejected(self):
        directory = self.copy_run()
        path = directory / "commit-loss-decision.json"
        save_json(path, {**load_json(path), "monotonic_ns": 10**30})
        self.assertEqual(assess_proxy_evidence(directory)["status"], "ERROR")

    def test_synthetic_timeout_cannot_prove_disconnect(self):
        directory = self.copy_run()
        path = directory / "mcp-1-http.jsonl"
        data = rows(path)
        for row in data:
            if row["event"] == "http_request_failed":
                row["error_type"] = "ReadTimeout"
        path.write_text("\n".join(json.dumps(r) for r in data) + "\n", encoding="utf-8")
        self.assertEqual(assess_proxy_evidence(directory)["status"], "ERROR")

    def test_normal_mcp_verifier_does_not_accept_error_as_success(self):
        with self.assertRaises(Exception):
            verify_mcp_evidence(Path(self.c["run_dir"]), self.c["run"])

    def test_readonly_recheck_preserves_all_evidence(self):
        directory = Path(self.c["run_dir"])
        hashes = lambda: {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob("*") if p.is_file()}
        before = hashes()
        self.assertEqual(recheck_ticket_run(directory)["status"], "PASS")
        self.assertEqual(hashes(), before)

    def test_retry_response_may_complete_before_lost_response(self):
        directory = self.copy_run()
        path = directory / "http-service-events.jsonl"
        data = rows(path)
        completions = [i for i, r in enumerate(data) if r["event"] == "request_completed" and r["path"] == "/tickets"]
        self.assertEqual(len(completions), 2)
        a, b = completions
        data[a], data[b] = data[b], data[a]
        for i, row in enumerate(data, 1):
            row["seq"] = i
        path.write_text("\n".join(json.dumps(r) for r in data) + "\n", encoding="utf-8")
        self.assertEqual(recheck_ticket_run(directory)["status"], "PASS")


@unittest.skipUnless(DEFAULT_BINARY.is_file(), "Pinned native Gitea binary required")
class GiteaVisibilityLossTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        runtime = GiteaRuntime(DEFAULT_INSTANCE_ROOT)
        with runtime, target_environment(runtime):
            cls.item = gitea_case(Path(cls.temp.name))

    def test_api_visible_before_real_drop(self):
        self.assertEqual(self.item["proxy_contract"]["status"], "PASS", self.item["proxy_contract"])
        self.assertEqual(self.item["result"]["status"], "ERROR")
        directory = Path(self.item["run_dir"])
        decision = load_json(directory / "commit-loss-decision.json")
        self.assertEqual(decision["source"], "gitea-api-readonly")
        self.assertIn(decision["record"], load_json(directory / "final.json")["issues"])

    def test_does_not_claim_internal_database_commit(self):
        directory = Path(self.item["run_dir"])
        names = [r["event"] for r in rows(directory / "commit-loss-events.jsonl")]
        self.assertIn("api_visibility_confirmed", names)
        self.assertNotIn("commit_confirmed", names)

    def test_missing_independent_api_evidence_is_not_covered(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(self.item["run_dir"])
            directory = Path(shutil.copytree(source, Path(temp) / source.name))
            proof = load_json(directory / "commit-loss-decision.json")
            (directory / proof["api_evidence"]).unlink()
            self.assertEqual(assess_proxy_evidence(directory)["status"], "INCONCLUSIVE")


if __name__ == "__main__":
    unittest.main()

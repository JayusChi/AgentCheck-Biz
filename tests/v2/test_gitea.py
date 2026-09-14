"""Gitea integration tests use the checksum-pinned, unmodified official binary."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import httpx
import jsonschema

from agentcheck_biz.adapters.contracts import PluginConfigurationError
from agentcheck_biz.adapters.gitea import gitea_plugin
from agentcheck_biz.adapters.gitea_client import GiteaClient, GiteaSettings
from agentcheck_biz.adapters.registry import PluginRegistry
from agentcheck_biz.checks import load_json
from agentcheck_biz.gitea_cases import validate_gitea_case
from agentcheck_biz.lifecycle import run_business_case
from agentcheck_biz.observers.gitea import normalize_issue
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.verifiers.gitea import recheck_gitea_run
from examples.gitea_target.demo import target_environment
from examples.gitea_target.runtime import BINARY_SHA256, DEFAULT_BINARY, GiteaRuntime


def case(name="G01"):
    return load_json(REPO_ROOT / f"cases/gitea/{name}.json")


class GiteaConfigurationTests(unittest.TestCase):
    def test_configuration_requires_explicit_test_instance_and_scoped_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PluginConfigurationError):
                GiteaSettings.from_environment()

    def test_schema_rejects_target_or_credential_fields_in_tool_request(self):
        for field in ("token", "repository", "owner", "operation_id"):
            value = case()
            value["request"][field] = "injected"
            with self.assertRaises(jsonschema.ValidationError):
                validate_gitea_case(value)

    def test_operation_marker_cannot_be_injected_through_body(self):
        value = case()
        value["request"]["body"] = "<!-- agentcheck:run=other;operation=issue-001 -->"
        with self.assertRaises(ValueError):
            validate_gitea_case(value)

    def test_object_schema_is_not_a_ticket_disguised_as_an_issue(self):
        from agentcheck_biz.runner import default_case
        with self.assertRaises(jsonschema.ValidationError):
            validate_gitea_case(default_case())
        self.assertNotIn("tenant_id", validate_gitea_case(case()))

    def test_runtime_source_participates_in_implementation_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "examples/gitea_target/runtime.py"
            source.parent.mkdir(parents=True)
            source.write_text("one", encoding="utf-8")
            with patch("agentcheck_biz.provenance.REPO_ROOT", root):
                old = implementation_digest()
                source.write_text("two", encoding="utf-8")
                self.assertNotEqual(old, implementation_digest())


@unittest.skipUnless(DEFAULT_BINARY.is_file(), "Official Gitea binary required: python scripts/fetch_gitea.py")
class GiteaIntegrationTests(unittest.TestCase):
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

    def run_case(self, name="G01", value=None, registry=None):
        return run_business_case(self.root / "runs", plugin_id="gitea", case=value or case(name), registry=registry)

    def test_official_version_and_permissions_are_real(self):
        self.assertEqual(hashlib.sha256(DEFAULT_BINARY.read_bytes()).hexdigest(), BINARY_SHA256)
        self.assertNotEqual(self.runtime.identity["pid"], os.getpid())
        outcome = self.run_case()
        target = load_json(Path(outcome["run_dir"]) / "gitea-target.json")
        path = "/api/v1/repos/" + target["repository"]["full_name"] + "/issues"
        with httpx.Client(base_url=self.runtime.origin, trust_env=False, timeout=3) as client:
            read_headers = {"Authorization": "token " + self.runtime.credentials["GITEA_OBSERVER_TOKEN"]}
            write_headers = {"Authorization": "token " + self.runtime.credentials["GITEA_EXECUTION_TOKEN"]}
            self.assertEqual(client.post(path, headers=read_headers, json={"title": "not allowed"}).status_code, 403)
            self.assertEqual(client.post("/api/v1/user/repos", headers=write_headers,
                                        json={"name": "not-allowed", "private": True}).status_code, 403)
            response = client.get(path, headers=read_headers, params={"state": "all", "type": "issues"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()), 1)

    def test_create_and_offline_recheck_need_no_live_server_calls(self):
        outcome = self.run_case()
        self.assertEqual(outcome["result"]["status"], "PASS", outcome["result"])
        directory = Path(outcome["run_dir"])
        before = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        with patch.object(httpx.AsyncClient, "request", side_effect=AssertionError("Offline recheck used network")):
            self.assertEqual(recheck_gitea_run(directory)["status"], "PASS")
        self.assertEqual(before, {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()})
        self.assertFalse(outcome["run"]["model_called"])

    def test_duplicate_operation_is_detected_without_modifying_gitea(self):
        outcome = self.run_case("G02")
        self.assertEqual(outcome["result"]["status"], "FAIL", outcome["result"])
        self.assertEqual(outcome["run"]["execution_status"], "completed")
        count = next(c for c in outcome["result"]["checks"] if c["check_id"] == "issue_count_for_operation")
        self.assertEqual(count["actual"], 2)

    def test_closed_issues_pagination_and_same_title_unrelated_operations(self):
        outcome = self.run_case("G03")
        self.assertEqual(outcome["result"]["status"], "PASS", outcome["result"])
        observed = load_json(Path(outcome["run_dir"]) / "observation-final.json")["data"]
        self.assertEqual(len(observed["pagination"]["pages"]), 3)
        self.assertEqual(observed["pagination"]["total_count"], 6)
        self.assertEqual(observed["issues"][-1]["state"], "closed")
        self.assertEqual(len({row["title"] for row in observed["issues"]}), 1)
        self.assertEqual(len(observed["detail_evidence"]), 6)

    def test_each_run_allocates_a_different_private_repository(self):
        outcomes = [self.run_case() for _ in range(2)]
        targets = [load_json(Path(outcome["run_dir"]) / "gitea-target.json")["repository"] for outcome in outcomes]
        self.assertNotEqual(targets[0]["id"], targets[1]["id"])
        self.assertNotEqual(targets[0]["full_name"], targets[1]["full_name"])
        self.assertTrue(all(target["private"] for target in targets))
        self.assertTrue(all(outcome["result"]["status"] == "PASS" for outcome in outcomes))

    def test_pagination_budget_exhaustion_is_error_not_zero_rows(self):
        value = case("G03")
        value["limits"]["max_pages"] = 1
        outcome = self.run_case(value=value)
        self.assertEqual(outcome["result"]["status"], "ERROR")
        observed = load_json(Path(outcome["run_dir"]) / "observation-initial.json")
        self.assertFalse(observed["complete"])
        self.assertIsNone(observed["data"])
        self.assertIn("pagination", observed["error"])

    def test_read_failure_does_not_accept_a_successful_create(self):
        original = GiteaClient.request
        wrote = False

        def unavailable(client, method, path, **kwargs):
            nonlocal wrote
            if client.role == "observer" and wrote:
                raise OSError("independent API read unavailable")
            result = original(client, method, path, **kwargs)
            if client.role == "execution":
                wrote = True
            return result

        with patch.object(GiteaClient, "request", unavailable):
            outcome = self.run_case()
        self.assertEqual(outcome["run"]["client_result"]["status"], "completed")
        self.assertEqual(outcome["result"]["status"], "ERROR")
        self.assertEqual(outcome["run"]["cleanup_status"], "completed")

    def test_invalid_observer_token_is_not_an_empty_repository(self):
        with patch.dict(os.environ, {"GITEA_OBSERVER_TOKEN": "f" * 40}):
            outcome = self.run_case()
        self.assertEqual(outcome["result"]["status"], "ERROR")
        self.assertIsNone(outcome["run"]["client_result"])

    def test_incomplete_malformed_and_foreign_pages_cannot_pass(self):
        original = GiteaClient.request
        for mutation in ("missing_total", "missing_next", "foreign_link", "repeat_page", "bad_row"):
            with self.subTest(mutation=mutation):
                first_page = None

                def corrupt(client, method, path, **kwargs):
                    nonlocal first_page
                    response = original(client, method, path, **kwargs)
                    if client.role != "observer" or not path.endswith("/issues"):
                        return response
                    response = deepcopy(response)
                    if mutation == "missing_total":
                        response["headers"]["x-total-count"] = None
                    elif mutation == "missing_next":
                        response["headers"]["link"] = None
                    elif mutation == "foreign_link":
                        response["headers"]["link"] = '<http://example.invalid/steal>; rel="next"'
                    elif mutation == "repeat_page":
                        if first_page is None:
                            first_page = response["body"]
                        else:
                            response["body"] = first_page
                    else:
                        response["body"][0]["repository"]["id"] = -1
                    return response

                with patch.object(GiteaClient, "request", corrupt):
                    outcome = self.run_case("G03")
                self.assertEqual(outcome["result"]["status"], "ERROR", outcome["result"])

    def test_saved_api_response_loss_or_corruption_invalidates_recheck(self):
        outcome = self.run_case()
        directory = Path(outcome["run_dir"])
        observed = load_json(directory / "observation-final.json")
        for filename in (observed["data"]["detail_evidence"][0], "gitea-cleanup.json", "case.json"):
            path = directory / filename
            original = path.read_bytes()
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(recheck_gitea_run(directory)["status"], "ERROR")
            path.write_bytes(original)
        self.assertEqual(recheck_gitea_run(directory)["status"], "PASS")

    def test_tokens_never_enter_run_evidence_or_settings_repr(self):
        outcome = self.run_case()
        tokens = list(self.runtime.credentials.values())
        for path in Path(outcome["run_dir"]).rglob("*"):
            if path.is_file():
                contents = path.read_bytes()
                for token in tokens:
                    self.assertNotIn(token.encode(), contents)
        for token in tokens:
            self.assertNotIn(token, repr(GiteaSettings.from_environment()))

    def test_version_mismatch_stops_before_repository_allocation(self):
        with patch.dict(os.environ, {"GITEA_EXPECTED_VERSION": "0.0.0"}):
            outcome = self.run_case()
        self.assertEqual(outcome["result"]["status"], "ERROR")
        self.assertFalse((Path(outcome["run_dir"]) / "gitea-target.json").exists())

    def test_owned_process_cleanup_keeps_other_instance_running(self):
        with GiteaRuntime(self.root / "second-instance") as other:
            self.assertNotEqual(self.runtime.origin, other.origin)
            self.assertNotEqual(self.runtime.credentials["GITEA_EXECUTION_TOKEN"], other.credentials["GITEA_EXECUTION_TOKEN"])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", other.port), timeout=.2)
        self.assertIsNone(self.runtime.process.poll())
        self.assertTrue(load_json(other.directory / "cleanup.json")["exited"])

    def test_deep_evidence_is_separate_from_bounded_native_work_path(self):
        if os.name == "nt":
            with self.assertRaisesRegex(ValueError, "short instance root"):
                GiteaRuntime(self.root / ("long-" + "x" * 140)).start()
        outcome = run_business_case(self.root / ("deep-evidence-" + "x" * 100), plugin_id="gitea", case=case())
        self.assertEqual(outcome["result"]["status"], "PASS", outcome["result"])
        self.assertEqual(self.runtime.child_env["GIT_CEILING_DIRECTORIES"], str(self.runtime.directory))


if __name__ == "__main__":
    unittest.main()

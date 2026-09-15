"""Exercise lifecycle boundaries with real file/SQLite state and failing adapters."""

from dataclasses import replace
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from agentcheck_biz.adapters.contracts import BusinessPlugin, Observation, PluginConfigurationError, RunContext
from agentcheck_biz.adapters.registry import PluginRegistry
from agentcheck_biz.checks import check_run, load_json
from agentcheck_biz.events import EventLog
from agentcheck_biz.lifecycle import run_business_case
from agentcheck_biz.observers.ticket_local import TicketStateObserver
from agentcheck_biz.runner import default_case, run_ticket_case
from agentcheck_biz.verifiers.ticket_local import TicketBusinessVerifier, recheck_ticket_run
from examples.ticket_agent.database import initialize_database
from examples.ticket_agent.service import OperationContext, TicketService
from examples.ticket_agent.tool_executor import TicketToolExecutor
from examples.ticket_agent.llm_agent import LiveTicketAgent, ModelConfig, ModelRunTimedOut


class FileFixture:
    """A synthetic protocol fixture, not an independent external product."""
    def __init__(self):
        self.cleaned = False
        self.executed = False
        self.verified = False
        self.prepare_error = False
        self.cleanup_error = False
        self.verifier_error = False
        self.bad_verdict = False
        self.after_write = lambda context: None

    def prepare(self, context, events):
        self.path = context.evidence_dir / "state.json"
        self.path.write_text(json.dumps({"run_id": context.run_id, "value": 0}), encoding="utf-8")
        if self.prepare_error:
            raise OSError("partial preparation failed")

    def execute(self, context, events):
        self.executed = True
        self.path.write_text(json.dumps({"run_id": context.run_id, "value": 1}), encoding="utf-8")
        self.after_write(context)
        return {"confirmed": True}

    def metadata(self):
        return {"tool_calls": int(self.executed)}

    def observe(self, context):
        data = load_json(self.path)
        return Observation("readonly-file:state.json", datetime.now(timezone.utc).isoformat(),
                           data["run_id"], context.operation_id, context.attempt_id, True, data)

    def cleanup(self, context, events):
        self.cleaned = True
        if self.cleanup_error:
            raise OSError("resource release failed")

    def verify(self, context, observation):
        self.verified = True
        if self.verifier_error:
            raise RuntimeError("verifier failed")
        passed = load_json(self.path)["value"] == 1 and not self.bad_verdict
        return {"status": "PASS", "exit_code": 0, "reason": "Value is one", "checks": [
            {"check_id": "value", "expected": 1, "actual": observation.data["value"],
             "passed": passed, "evidence": ["state.json"]}]}

    def plugin(self, case, options):
        return BusinessPlugin("file-fixture", 1, case, "operation-one", 30, "fixture",
                              {"mode": "offline protocol fixture"}, self, self, self, self)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = FileFixture()
        self.registry = PluginRegistry()
        self.registry.register("file-fixture", self.fixture.plugin)

    def run_fixture(self):
        return run_business_case(self.root / "runs", plugin_id="file-fixture", case={"desired_value": 1}, registry=self.registry)

    def test_non_ticket_plugin_uses_same_lifecycle_without_ticket_case_fields(self):
        result = self.run_fixture()
        self.assertEqual(result["result"]["status"], "PASS")
        self.assertTrue(self.fixture.cleaned and self.fixture.verified)
        saved = load_json(Path(result["run_dir"]) / "observation-final.json")
        self.assertEqual(saved["data"]["value"], 1)
        self.assertEqual(saved["schema_version"], 1)
        self.assertNotIn("tickets", saved["data"])

    def test_unknown_plugin_is_diagnostic_and_allocates_no_environment(self):
        with self.assertRaisesRegex(PluginConfigurationError, "Unknown registered plugin"):
            run_business_case(self.root / "runs", plugin_id="os.system", case={}, registry=self.registry)
        self.assertFalse((self.root / "runs").exists())

    def test_missing_observer_is_rejected_before_preparation(self):
        registry = PluginRegistry()
        registry.register("file-fixture", lambda case, opts: replace(self.fixture.plugin(case, opts), observer=None))
        with self.assertRaisesRegex(PluginConfigurationError, "observer capability"):
            run_business_case(self.root / "runs", plugin_id="file-fixture", case={}, registry=registry)
        self.assertFalse((self.root / "runs").exists())
        self.assertFalse(self.fixture.executed)

    def test_duplicate_registration_and_invalid_options_rejected(self):
        with self.assertRaises(PluginConfigurationError):
            self.registry.register("file-fixture", self.fixture.plugin)
        for options in ([], {"unknown": True}, {"inject_fault": "false"}):
            with self.subTest(options=options), self.assertRaises(PluginConfigurationError):
                run_business_case(self.root / "runs", plugin_id="ticket-local", case=default_case(), options=options)
        self.assertFalse((self.root / "runs").exists())

    def test_wrong_run_id_is_saved_and_cannot_reach_verifier(self):
        self.fixture.after_write = lambda context: self.fixture.path.write_text('{"run_id":"another-run","value":1}', encoding="utf-8")
        result = self.run_fixture()
        directory = Path(result["run_dir"])
        self.assertEqual(result["result"]["status"], "ERROR")
        self.assertEqual(load_json(directory / "observation-final.json")["run_id"], "another-run")
        self.assertFalse((directory / "final.json").exists())
        self.assertTrue(self.fixture.cleaned)
        self.assertFalse(self.fixture.verified)

    def test_missing_state_is_error_not_an_empty_success(self):
        self.fixture.after_write = lambda context: self.fixture.path.unlink()
        result = self.run_fixture()
        observation = load_json(Path(result["run_dir"]) / "observation-final.json")
        self.assertEqual(result["result"]["status"], "ERROR")
        self.assertFalse(observation["complete"])
        self.assertIsNone(observation["data"])
        self.assertIn("FileNotFoundError", observation["error"])
        self.assertTrue(self.fixture.cleaned)

    def test_partial_observation_is_inconclusive_and_retains_reason(self):
        original = self.fixture.observe
        def partial(context):
            value = original(context)
            return replace(value, complete=False, error="pagination incomplete", failure_status="INCONCLUSIVE") if self.fixture.executed else value
        with patch.object(self.fixture, "observe", side_effect=partial):
            result = self.run_fixture()
        self.assertEqual(result["result"]["status"], "INCONCLUSIVE")
        self.assertIn("pagination incomplete", result["result"]["reason"])
        self.assertFalse((Path(result["run_dir"]) / "final.json").exists())

    def test_cleanup_failure_overrides_otherwise_passing_business_evidence(self):
        self.fixture.cleanup_error = True
        result = self.run_fixture()
        self.assertEqual(result["result"]["status"], "ERROR")
        self.assertEqual(result["run"]["cleanup_status"], "error")
        self.assertEqual(load_json(Path(result["run_dir"]) / "final.json")["value"], 1)
        self.assertFalse(self.fixture.verified)
        self.assertIn("cleanup_error", [item["event"] for item in result["run"]["errors"]])

    def test_partial_preparation_failure_still_cleans_and_keeps_evidence(self):
        self.fixture.prepare_error = True
        result = self.run_fixture()
        self.assertEqual(result["result"]["status"], "ERROR")
        self.assertFalse(self.fixture.executed)
        self.assertTrue(self.fixture.cleaned)
        self.assertTrue((Path(result["run_dir"]) / "observation-final.json").exists())

    def test_verifier_exception_and_contradictory_pass_are_errors(self):
        for attribute in ("verifier_error", "bad_verdict"):
            with self.subTest(attribute=attribute):
                setattr(self.fixture, attribute, True)
                result = self.run_fixture()
                self.assertEqual(result["result"]["status"], "ERROR")
                self.assertEqual(result["run"]["execution_status"], "error")
                self.assertTrue((Path(result["run_dir"]) / "checks.json").exists())
                setattr(self.fixture, attribute, False)

    def test_metadata_cannot_override_execution_failure(self):
        self.fixture.prepare_error = True
        with patch.object(self.fixture, "metadata", return_value={"tool_calls": 0, "execution_status": "completed"}):
            result = self.run_fixture()
        self.assertEqual(result["result"]["status"], "ERROR")
        self.assertNotEqual(result["run"]["execution_status"], "completed")
        self.assertTrue(self.fixture.cleaned)

    def test_malformed_observation_identity_schema_timestamp_or_completeness_fails(self):
        original = self.fixture.observe
        changes = [{"operation_id": "other"}, {"attempt_id": "other"}, {"schema_version": 2},
                   {"collected_at": "2026-09-08T12:00:00"}, {"complete": 1},
                   {"complete": False, "error": None}, {"complete": True, "data": None}]
        for fields in changes:
            with self.subTest(fields=fields), patch.object(self.fixture, "observe", side_effect=lambda c: replace(original(c), **fields)):
                result = self.run_fixture()
                self.assertEqual(result["result"]["status"], "ERROR")

    def test_ticket_observer_does_not_create_missing_database(self):
        directory = self.root / "missing"
        directory.mkdir()
        context = RunContext("missing", "op", "attempt", time.time() + 10, directory)
        value = TicketStateObserver().observe(context)
        self.assertFalse(value.complete)
        self.assertIsNone(value.data)
        self.assertFalse((directory / "business.sqlite").exists())

    def test_ticket_verifier_reobserves_database_instead_of_trusting_saved_observation(self):
        result = run_ticket_case(self.root, app_version="fixed", inject_fault=False)
        directory = Path(result["run_dir"])
        context = RunContext(**result["run"]["context"])
        observation = Observation(**load_json(directory / "observation-final.json"))
        with sqlite3.connect(directory / "business.sqlite") as db:
            db.execute("UPDATE tickets SET customer_id='wrong-customer'")
        self.assertEqual(TicketBusinessVerifier().verify(context, observation)["status"], "FAIL")
        self.assertEqual(check_run(directory)["status"], "FAIL")

    def test_retry_context_keeps_operation_and_deadline_with_distinct_call_attempts(self):
        result = run_ticket_case(self.root, app_version="fixed", inject_fault=True)
        events = [json.loads(line) for line in (Path(result["run_dir"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        calls = [item for item in events if item["event"] == "tool_called"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({item["attempt_id"] for item in calls}), 2)
        self.assertEqual({item["operation_id"] for item in calls}, {result["run"]["context"]["operation_id"]})
        self.assertEqual({item["deadline"] for item in calls}, {result["run"]["context"]["deadline"]})

    def test_expired_deadline_blocks_tool_before_write(self):
        directory = self.root / "expired"
        directory.mkdir()
        initialize_database(directory / "business.sqlite")
        events = EventLog(directory / "events.jsonl", "expired")
        context = RunContext("expired", "op", "attempt", time.time() - 1, directory)
        executor = TicketToolExecutor(TicketService(directory / "business.sqlite"), OperationContext("tenant", "op"), events, run_context=context)
        with self.assertRaises(TimeoutError):
            executor.create_ticket(customer_id="C", device_id="D", description="repair")
        self.assertEqual(executor.call_count, 0)
        with sqlite3.connect(directory / "business.sqlite") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tickets").fetchone()[0], 0)

    def test_source_digest_tracks_nested_adapter_implementation(self):
        from agentcheck_biz.provenance import implementation_digest
        path = self.root / "agentcheck_biz/adapters/plugin.py"
        path.parent.mkdir(parents=True)
        path.write_text("version=1", encoding="utf-8")
        with patch("agentcheck_biz.provenance.REPO_ROOT", self.root):
            before = implementation_digest()
            path.write_text("version=2", encoding="utf-8")
            self.assertNotEqual(before, implementation_digest())

    def test_recheck_rejects_missing_or_foreign_saved_observation(self):
        result = run_ticket_case(self.root, app_version="fixed", inject_fault=False)
        directory = Path(result["run_dir"])
        path = directory / "observation-final.json"
        original = path.read_text(encoding="utf-8")
        changed = json.loads(original)
        changed["run_id"] = "foreign"
        path.write_text(json.dumps(changed), encoding="utf-8")
        self.assertEqual(recheck_ticket_run(directory)["status"], "ERROR")
        path.unlink()
        self.assertEqual(recheck_ticket_run(directory)["status"], "ERROR")
        path.write_text(original, encoding="utf-8")
        self.assertEqual(recheck_ticket_run(directory)["status"], "PASS")

    def test_verifier_interruption_retains_inconclusive_report(self):
        with patch.object(self.fixture, "verify", side_effect=KeyboardInterrupt):
            result = self.run_fixture()
        self.assertEqual(result["result"]["status"], "INCONCLUSIVE")
        self.assertEqual(load_json(Path(result["run_dir"]) / "checks.json")["status"], "INCONCLUSIVE")

    def test_expired_context_does_not_send_a_model_request(self):
        directory = self.root / "expired-model"
        directory.mkdir()
        context = RunContext(directory.name, "op", "attempt", time.time() - 1, directory)
        create = AsyncMock()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        agent = LiveTicketAgent(ModelConfig())
        with self.assertRaises(ModelRunTimedOut):
            asyncio.run(agent.run_async("test", SimpleNamespace(run_context=context),
                EventLog(directory / "events.jsonl", directory.name), directory, client=client))
        create.assert_not_awaited()
        self.assertEqual(agent.metadata["model_request_attempts"], 0)

    def test_pending_model_request_is_cancelled_at_shared_deadline(self):
        directory = self.root / "model-deadline"
        directory.mkdir()
        context = RunContext(directory.name, "op", "attempt", time.time() + .2, directory)
        async def pending(**kwargs):
            await asyncio.Future()
        create = AsyncMock(side_effect=pending)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        agent = LiveTicketAgent(ModelConfig(timeout_seconds=90))
        async def run():
            return await asyncio.wait_for(agent.run_async("test", SimpleNamespace(run_context=context),
                EventLog(directory / "events.jsonl", directory.name), directory, client=client), timeout=2)
        with self.assertRaises(ModelRunTimedOut):
            asyncio.run(run())
        self.assertEqual(agent.metadata["model_responses"], 0)


if __name__ == "__main__":
    unittest.main()

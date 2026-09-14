"""D24 real process kills, persistence, fail-closed evidence and owned cleanup."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.checks import load_json
from agentcheck_biz.events import EventLog
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from agentcheck_biz.service_crash.__main__ import suite
from agentcheck_biz.service_crash.observer import read_state
from agentcheck_biz.service_crash.runner import run_case
from agentcheck_biz.service_crash.runtime import CrashRuntime
from agentcheck_biz.service_crash.server import open_persistent_data
from agentcheck_biz.service_crash.verify import recheck


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_rows(path, data):
    for seq, row in enumerate(data, 1):
        row["seq"] = seq
    path.write_text("".join(json.dumps(r) + "\n" for r in data), encoding="utf-8")


class ServiceCrashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.summary = suite(Path(cls.temp.name))
        cls.items = {(r["point"], r["max_tool_calls"]): r for r in cls.summary["runs"]}

    def directory(self, point="after_commit", maximum=6):
        return Path(self.items[(point, maximum)]["run_dir"])

    def clone(self, point="after_commit"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = self.directory(point)
        return Path(shutil.copytree(source, Path(temp.name) / source.name))

    def instance(self, directory, generation):
        return next(p for p in (directory / "instances").iterdir() if load_json(p / "allocation.json")["generation"] == generation)

    def test_all_three_real_crash_points_and_budget_case(self):
        self.assertEqual((self.summary["status"], self.summary["covered"]), ("PASS", 4), self.summary)

    def test_before_service_gate_sends_no_business_request(self):
        directory = self.directory("before_service")
        service = rows(self.instance(directory, 1) / "service-events.jsonl")
        self.assertFalse(any(r["event"] == "request_received" and r["path"] == "/tickets" for r in service))
        self.assertEqual(self.items[("before_service", 6)]["persistence"], "not_entered")
        trace = [r["row"] for r in rows(directory / "recovery-events.jsonl")]
        self.assertEqual(next(r["kind"] for r in trace if r["event"] == "outcome"), "not_forwarded")

    def test_uncommitted_ticket_and_mapping_really_rollback(self):
        directory = self.directory("before_commit")
        barrier = load_json(self.instance(directory, 1) / "barrier.json")
        restarted = load_json(directory / "observation-after_restart.json")["data"]
        final = load_json(directory / "observation-final.json")["data"]
        self.assertTrue(barrier["in_transaction"])
        self.assertEqual(barrier["mapping"]["ticket_id"], barrier["ticket"]["ticket_id"])
        self.assertNotIn(barrier["ticket"], restarted["tickets"])
        self.assertNotIn(barrier["mapping"], restarted["idempotency_keys"])
        self.assertNotIn(barrier["ticket"], final["tickets"])
        self.assertEqual(self.items[("before_commit", 6)]["restart_resource_count"], 0)

    def test_committed_ticket_and_mapping_survive_without_reseeding(self):
        directory = self.directory()
        barrier = load_json(self.instance(directory, 1) / "barrier.json")
        restarted = load_json(directory / "observation-after_restart.json")["data"]
        self.assertIn(barrier["ticket"], restarted["tickets"])
        self.assertTrue(any(r["ticket_id"] == barrier["ticket"]["ticket_id"] for r in restarted["idempotency_keys"]))
        self.assertEqual(self.items[("after_commit", 6)]["persistence"], "committed_retained")

    def test_same_data_file_new_instance_and_no_new_operation(self):
        directory = self.directory()
        first, second = [load_json(self.instance(directory, n) / "ready.json") for n in (1, 2)]
        self.assertNotEqual(first["instance_id"], second["instance_id"])
        self.assertEqual(first["data_dir"], second["data_dir"])
        events = rows(directory / "events.jsonl")
        healthy = [r for r in events if r["event"] == "generation_healthy"]
        self.assertEqual(healthy[0]["database_file_id"], healthy[1]["database_file_id"])
        trace = [r["row"] for r in rows(directory / "recovery-events.jsonl")]
        self.assertEqual(len({r["operation_id"] for r in trace}), 1)
        self.assertEqual(len({r["budget"]["deadline"] for r in trace}), 1)

    def test_restart_does_not_replenish_exhausted_tool_budget(self):
        item = self.items[("after_commit", 1)]
        self.assertEqual((item["tool_calls"], item["resource_count"], item["client_status"], item["business_status"]),
                         (1, 1, "needs_verification", "INCONCLUSIVE"))

    def test_all_owned_processes_exited_with_evidence_retained(self):
        for item in self.summary["runs"]:
            directory = Path(item["run_dir"])
            cleanup = load_json(directory / "cleanup.json")
            self.assertEqual((cleanup["allocated"], cleanup["exited"], cleanup["failures"]), (2, 2, []))
            self.assertTrue((directory / "data/business.sqlite").is_file())
            for n in (1, 2):
                instance = self.instance(directory, n)
                self.assertTrue((instance / "process.log").is_file())
                self.assertTrue(load_json(instance / "exit.json")["exited"])

    def test_recheck_never_changes_saved_evidence(self):
        for item in self.summary["runs"]:
            directory = Path(item["run_dir"])
            before = {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
            self.assertEqual(recheck(directory)["status"], "PASS")
            self.assertEqual(before, {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob("*") if p.is_file()})

    def test_missing_barrier_is_not_covered(self):
        directory = self.clone()
        (self.instance(directory, 1) / "barrier.json").unlink()
        self.assertEqual(recheck(directory)["coverage"], "not_covered")

    def test_foreign_barrier_attempt_is_rejected(self):
        directory = self.clone()
        path = self.instance(directory, 1) / "barrier.json"
        data = load_json(path)
        data["attempt_id"] = "foreign"
        save_json(path, data)
        self.assertEqual(recheck(directory)["coverage"], "not_covered")

    def test_forged_exit_pid_cannot_pass(self):
        directory = self.clone()
        path = self.instance(directory, 1) / "exit.json"
        data = load_json(path)
        data["pid"] += 100
        save_json(path, data)
        self.assertEqual(recheck(directory)["coverage"], "not_covered")

    def test_barrier_release_cannot_be_misreported_as_crash_window(self):
        directory = self.clone()
        save_json(self.instance(directory, 1) / "release.json", {})
        self.assertEqual(recheck(directory)["coverage"], "not_covered")

    def test_reseed_on_restart_is_detected(self):
        directory = self.clone()
        path = self.instance(directory, 2) / "service-events.jsonl"
        data = rows(path)
        next(r for r in data if r["event"] == "database_resumed")["event"] = "database_seeded"
        write_rows(path, data)
        self.assertIn("reseeded", recheck(directory)["reason"])

    def test_missing_real_health_exchange_is_detected(self):
        directory = self.clone()
        path = self.instance(directory, 2) / "service-events.jsonl"
        data = [r for r in rows(path) if not (r["event"] == "request_received" and r["path"] == "/health")]
        write_rows(path, data)
        self.assertIn("actual HTTP", recheck(directory)["reason"])

    def test_missing_recovery_request_in_service_log_is_detected(self):
        directory = self.clone()
        path = self.instance(directory, 2) / "service-events.jsonl"
        data = [r for r in rows(path) if not (r["event"] == "request_received" and r["path"] == "/tickets")]
        write_rows(path, data)
        self.assertIn("missing from service", recheck(directory)["reason"])

    def test_unreadable_independent_observation_cannot_pass(self):
        directory = self.clone()
        (directory / "observation-after_restart.json").unlink()
        self.assertEqual(recheck(directory)["business_status"], "INCONCLUSIVE")

    def test_budget_reset_after_restart_is_detected(self):
        directory = self.clone()
        path = directory / "recovery-events.jsonl"
        data = rows(path)
        calls = [r["row"] for r in data if r["row"]["event"] == "call"]
        calls[-1]["budget"]["tool_calls"] = 1
        write_rows(path, data)
        self.assertEqual(recheck(directory)["coverage"], "not_covered")

    def test_changed_operation_after_restart_is_detected(self):
        directory = self.clone()
        path = directory / "recovery-events.jsonl"
        data = rows(path)
        calls = [r["row"] for r in data if r["row"]["event"] == "call"]
        calls[-1]["operation_id"] = "new-operation"
        write_rows(path, data)
        self.assertEqual(recheck(directory)["coverage"], "not_covered")

    def test_changed_persisted_database_is_detected(self):
        directory = self.clone()
        with sqlite3.connect(directory / "data/business.sqlite") as connection:
            connection.execute("DELETE FROM idempotency_keys WHERE tenant_id = 'tenant-A'")
            connection.execute("DELETE FROM tickets WHERE tenant_id = 'tenant-A'")
        self.assertEqual(recheck(directory)["coverage"], "not_covered")

    def test_failed_cleanup_cannot_pass(self):
        directory = self.clone()
        path = directory / "cleanup.json"
        data = load_json(path)
        data["exited"] = 1
        save_json(path, data)
        self.assertEqual(recheck(directory)["status"], "INCONCLUSIVE")


class CrashFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def runtime(self):
        run_id = "owned-" + uuid4().hex
        directory = self.root / run_id
        directory.mkdir()
        context = RunContext(run_id, "repair-001", "attempt", time.time() + 45, directory)
        runtime = CrashRuntime(context, load_json(REPO_ROOT / "cases/tickets/full/T01.json"),
                               EventLog(directory / "events.jsonl", run_id))
        self.addCleanup(runtime.close)
        return runtime

    def assert_preserved(self, item):
        directory = Path(item["run_dir"])
        cleanup = load_json(directory / "cleanup.json")
        self.assertEqual(cleanup["allocated"], cleanup["exited"])
        self.assertFalse(cleanup["failures"])
        self.assertTrue((directory / "data/business.sqlite").is_file())
        self.assertTrue(list(directory.glob("instances/*/process.log")))
        self.assertEqual(item["coverage"], "not_covered")

    def test_foreign_allocation_never_kills_another_service(self):
        first, second = self.runtime(), self.runtime()
        first.start()
        second.start()
        with self.assertRaises(ValueError):
            first.stop(second.current, "foreign")
        self.assertIsNone(second.current.process.poll())
        first.close()
        self.assertIsNone(second.current.process.poll())

    def test_changed_ready_identity_refuses_fault_kill(self):
        runtime = self.runtime()
        runtime.start()
        expected = runtime.arm("before_service", "attempt/call-1-test")
        original = load_json(runtime.current.directory / "ready.json")
        save_json(runtime.current.directory / "ready.json", {**original, "instance_id": "foreign"})
        with self.assertRaises(ValueError):
            runtime.crash(expected, expected)
        self.assertIsNone(runtime.current.process.poll())

    def test_mutated_allocation_owner_is_rejected(self):
        runtime = self.runtime()
        runtime.start()
        foreign = replace(runtime.current, owner_pid=-1)
        with self.assertRaises(ValueError):
            runtime.stop(foreign, "invalid_owner")
        self.assertIsNone(runtime.current.process.poll())

    def test_restart_refused_while_old_service_alive(self):
        runtime = self.runtime()
        runtime.start()
        with self.assertRaises(RuntimeError):
            runtime.start()
        self.assertEqual(len(runtime.allocations), 1)

    def test_barrier_timeout_cleans_children_and_keeps_database(self):
        from agentcheck_biz.commit_loss.barrier import wait_message
        def missing_gate(path, expected, deadline):
            return wait_message(path.with_name("missing-barrier.json"), expected, min(deadline, time.time() + .05))
        with patch("agentcheck_biz.service_crash.runner.wait_message", side_effect=missing_gate):
            item = run_case(self.root, "before_commit")
        self.assert_preserved(item)
        self.assertEqual(item["reason"], "TimeoutError")

    def test_exception_after_kill_still_cleans_original_process(self):
        original = CrashRuntime.crash
        def fail_after_kill(runtime, *args):
            original(runtime, *args)
            raise RuntimeError("test failure after kill")
        with patch.object(CrashRuntime, "crash", fail_after_kill):
            item = run_case(self.root, "after_commit")
        self.assert_preserved(item)

    def test_restart_health_failure_cleans_new_process(self):
        from agentcheck_biz.adapters.http_transport import HttpTransport
        original = HttpTransport.request
        def fail_probe(transport, method, path, **kwargs):
            if kwargs["request_id"] == "generation-2-health":
                raise TimeoutError("test restart health deadline")
            return original(transport, method, path, **kwargs)
        with patch.object(HttpTransport, "request", fail_probe):
            item = run_case(self.root, "after_commit")
        self.assert_preserved(item)
        self.assertEqual(load_json(Path(item["run_dir"]) / "cleanup.json")["allocated"], 2)

    def test_observer_failure_is_not_empty_or_rollback(self):
        original = read_state
        count = 0
        def unreadable_once(path):
            nonlocal count
            count += 1
            if count == 4:
                raise sqlite3.OperationalError("test independent observer unavailable")
            return original(path)
        with patch("agentcheck_biz.service_crash.observer.read_state", unreadable_once):
            item = run_case(self.root, "after_commit")
        self.assert_preserved(item)
        self.assertEqual(item["persistence"], "unverified")
        observation = load_json(Path(item["run_dir"]) / "observation-after_restart.json")
        self.assertFalse(observation["complete"])
        self.assertIsNone(observation["data"])

    def test_missing_resume_database_is_never_created(self):
        data = self.root / "data"
        data.mkdir()
        config = {"data_dir": str(data), "generation": 2, "run_id": "r"}
        with self.assertRaises(ValueError):
            open_persistent_data(config, EventLog(self.root / "events.jsonl", "r"))
        self.assertFalse((data / "business.sqlite").exists())

    def test_initial_generation_refuses_existing_database(self):
        data = self.root / "data"
        data.mkdir()
        database = data / "business.sqlite"
        database.write_bytes(b"do not replace")
        with self.assertRaises(ValueError):
            open_persistent_data({"data_dir": str(data), "generation": 1}, EventLog(self.root / "events.jsonl", "r"))
        self.assertEqual(database.read_bytes(), b"do not replace")

    def test_unsupported_fault_does_not_allocate_anything(self):
        with self.assertRaises(ValueError):
            run_case(self.root, "random_delay")
        self.assertEqual(list(self.root.iterdir()), [])

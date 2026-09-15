"""Real PostgreSQL persistence, process isolation and fail-closed recovery."""

from copy import deepcopy
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from agentcheck_biz.checks import load_json
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.persistence.demo import suite, Experiment
from agentcheck_biz.persistence.graph import read_snapshot, checkpointer, build, config, validate_snapshot
from agentcheck_biz.persistence.runtime import PostgresRuntime
from agentcheck_biz.persistence.store import Store, BindingError, new_manifest, validate_manifest, package_versions, digest
from agentcheck_biz.persistence.verify import recheck


class CheckpointProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.result = suite(Path(cls.temp.name))
        if cls.result["status"] != "PASS":
            raise AssertionError(cls.result)
        cls.normal = {s["label"]: s for s in cls.result["normal"]["steps"]}
        cls.gap = {s["label"]: s for s in cls.result["gap"]["steps"]}

    def test_real_postgres_and_all_acceptance_assertions(self):
        self.assertEqual(self.result["postgres"]["version"], "17.11")
        self.assertEqual(len(self.result["assertions"]), 5)
        self.assertTrue(all(self.result["assertions"].values()))

    def test_closed_agent_then_another_process_reads_same_checkpoint(self):
        first, read = self.normal["start"], self.normal["read"]
        self.assertTrue(first["exited"])
        self.assertEqual(first["exit_code"], 0)
        self.assertNotEqual(first["pid"], read["pid"])
        self.assertEqual(first["result"]["checkpoint_id"], read["result"]["checkpoint_id"])
        self.assertEqual(first["result"]["values"], read["result"]["values"])

    def test_resumption_keeps_job_thread_operation_and_environment(self):
        first, resumed = self.normal["start"]["result"], self.normal["resume"]["result"]
        for key in ("job_id", "thread_id", "operation_id", "experiment_id", "environment_id", "run_id"):
            self.assertEqual(first["values"][key], resumed["values"][key])
        self.assertNotEqual(first["attempt_id"], resumed["attempt_id"])
        self.assertEqual(resumed["job_state"], "finished")

    def test_network_create_is_not_replayed_on_resume(self):
        self.assertEqual(self.normal["resume"]["result"]["values"]["create_calls"], 1)
        directory = Path(self.result["normal"]["run_dir"])
        rows = [json.loads(line) for line in (directory / "http-service-events.jsonl").read_text(encoding="utf8").splitlines()]
        posts = [r for r in rows if r["event"] == "request_received" and r["path"] == "/tickets" and r["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.result["normal"]["resource_count"], 1)

    def test_database_unavailable_never_creates_empty_progress(self):
        result = self.normal["database_unavailable"]
        self.assertEqual((result["result"]["status"], result["exit_code"]), ("ERROR", 3))
        self.assertNotIn("values", result["result"])

    def test_postgres_restart_preserves_checkpoint_and_system_identity(self):
        self.assertEqual(self.normal["after_postgres_restart"]["result"]["checkpoint_id"], self.normal["read"]["result"]["checkpoint_id"])
        self.assertEqual(len(self.result["cleanup"]), 2)
        self.assertNotEqual(self.result["cleanup"][0]["pid"], self.result["cleanup"][1]["pid"])
        self.assertTrue(all(p["exited"] for p in self.result["cleanup"]))

    def test_foreign_thread_is_refused_before_read(self):
        self.assertEqual(self.normal["foreign_thread"]["result"]["error_type"], "BindingError")

    def test_missing_checkpoint_is_not_a_new_task(self):
        self.assertEqual(self.result["foreign"]["steps"][0]["result"]["error_type"], "BindingError")
        self.assertEqual(self.result["job_states"], {"finished": 1, "queued": 1, "waiting_verification": 1})

    def test_business_commit_and_checkpoint_gap_are_distinct(self):
        self.assertEqual(self.gap["start"]["result"]["status"], "INCONCLUSIVE")
        self.assertEqual(self.gap["read"]["result"]["values"]["phase"], "prepared")
        self.assertIsNone(self.gap["read"]["result"]["values"]["ticket"])
        self.assertEqual(self.result["gap"]["resource_count"], 1)

    def test_gap_does_not_blindly_repeat_side_effect(self):
        self.assertEqual(self.gap["resume"]["result"]["status"], "ERROR")
        directory = Path(self.result["gap"]["run_dir"])
        self.assertFalse((directory / "resume/http-client.jsonl").exists())

    def test_terminal_job_cannot_be_resumed_again(self):
        self.assertEqual(self.normal["terminal_resume"]["result"]["error_type"], "BindingError")

    def test_saved_export_has_real_checkpoint_tables_and_migrations(self):
        directory = Path(self.result["summary_path"]).parent
        data = load_json(directory / "postgres-snapshot.json")
        self.assertEqual(digest(data), self.result["database_snapshot_sha256"])
        self.assertTrue(data["checkpoint_blobs"] and data["checkpoints"])
        self.assertEqual(len(data["ac_jobs"]), 3)
        self.assertEqual([r["v"] for r in data["checkpoint_migrations"]], list(range(10)))

    def test_deterministic_graph_sends_no_model_requests(self):
        self.assertEqual(self.result["model_requests"], 0)
        for record in (self.result["normal"], self.result["gap"], self.result["foreign"]):
            self.assertTrue(all(s["result"]["model_requests"] == 0 for s in record["steps"]))

    def test_saved_evidence_rechecks_without_postgres_or_agent_running(self):
        result = recheck(Path(self.result["summary_path"]).parent)
        self.assertEqual(result["status"], "PASS", result)

    def test_missing_postgres_export_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "copy"
            shutil.copytree(Path(self.result["summary_path"]).parent, directory)
            (directory / "postgres-snapshot.json").unlink()
            self.assertEqual(recheck(directory)["status"], "ERROR")

    def test_changed_saved_agent_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            original = Path(self.result["summary_path"]).parent
            directory = Path(shutil.copytree(original, Path(temp) / "copy"))
            relative = Path(self.normal["read"]["result_path"]).relative_to(original)
            result = load_json(directory / relative)
            result["values"]["thread_id"] = str(uuid4())
            (directory / relative).write_text(json.dumps(result), encoding="utf8")
            summary = load_json(directory / "summary.json")
            next(s for s in summary["normal"]["steps"] if s["label"] == "read")["result"] = result
            (directory / "summary.json").write_text(json.dumps(summary), encoding="utf8")
            self.assertEqual(recheck(directory)["status"], "ERROR")

    def test_corrupted_pg_channel_cannot_be_hidden_by_updated_export_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            original = Path(self.result["summary_path"]).parent
            directory = Path(shutil.copytree(original, Path(temp) / "copy"))
            database = load_json(directory / "postgres-snapshot.json")
            summary = load_json(directory / "summary.json")
            row = next(b for b in database["checkpoint_blobs"] if b["channel"] == "ticket")
            row["blob"] = "\\x81a97469636b65745f6964a7666f726569676e"
            summary["database_snapshot_sha256"] = digest(database)
            (directory / "postgres-snapshot.json").write_text(json.dumps(database), encoding="utf8")
            (directory / "summary.json").write_text(json.dumps(summary), encoding="utf8")
            self.assertEqual(recheck(directory)["status"], "ERROR")


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.pg = PostgresRuntime(Path(cls.temp.name)).start()
        cls.addClassCleanup(cls.pg.close)
        cls.store = Store(cls.pg.dsn)
        cls.store.setup()

    def make(self):
        directory = Path(self.temp.name) / ("run-" + uuid4().hex)
        manifest = new_manifest(directory, load_json(REPO_ROOT / "cases/tickets/full/T01.json"))
        self.store.create(manifest)
        return manifest

    def test_migration_is_idempotent(self):
        before = self.store.export()
        self.store.setup()
        self.assertEqual(before, self.store.export())

    def test_modified_migration_version_is_rejected(self):
        with self.store.connect() as conn:
            checksum = conn.execute("SELECT sha256 FROM ac_schema_versions WHERE version=1").fetchone()["sha256"]
            conn.execute("UPDATE ac_schema_versions SET sha256='foreign'")
        try:
            with self.assertRaises(BindingError):
                self.store.setup()
        finally:
            with self.store.connect() as conn:
                conn.execute("UPDATE ac_schema_versions SET sha256=%s", (checksum,))

    def test_newer_checkpoint_schema_is_not_silently_accepted(self):
        with self.store.connect() as conn:
            conn.execute("INSERT INTO checkpoint_migrations VALUES(999)")
        try:
            with self.assertRaises(BindingError):
                self.store.setup()
        finally:
            with self.store.connect() as conn:
                conn.execute("DELETE FROM checkpoint_migrations WHERE v=999")

    def test_persisted_identity_rejects_each_foreign_identifier(self):
        manifest = self.make()
        for key in ("job_id", "thread_id", "experiment_id", "environment_id"):
            forged = deepcopy(manifest)
            forged[key] = str(uuid4())
            with self.subTest(key=key), self.assertRaises(BindingError):
                self.store.get(forged)

    def test_request_change_cannot_reuse_checkpoint_identity(self):
        manifest = self.make()
        manifest["case"]["request"]["description"] = "another request"
        with self.assertRaises(BindingError):
            self.store.get(manifest)

    def test_forged_graph_or_storage_version_is_rejected(self):
        manifest = self.make()
        for key in ("graph_version", "storage_version", "checkpoint_namespace"):
            forged = deepcopy(manifest)
            forged[key] = "foreign"
            with self.subTest(key=key), self.assertRaises(BindingError):
                validate_manifest(forged)

    def test_new_experiments_get_distinct_threads_and_environments(self):
        one, two = self.make(), self.make()
        self.assertEqual(one["operation_id"], two["operation_id"])
        for key in ("job_id", "thread_id", "environment_id", "experiment_id", "run_id"):
            self.assertNotEqual(one[key], two[key])

    def test_no_second_running_attempt_and_transaction_rolls_back(self):
        manifest = self.make()
        attempt = self.store.begin(manifest)
        before = self.store.export()
        with self.assertRaises(BindingError):
            self.store.begin(manifest)
        self.assertEqual(before, self.store.export())
        self.assertEqual(str(self.store.get(manifest)["attempt_id"]), attempt)

    def test_stale_attempt_cannot_finish_job(self):
        manifest = self.make()
        attempt = self.store.begin(manifest)
        self.store.finish_attempt(manifest, attempt, "interrupted", None, "test")
        next_attempt = self.store.begin(manifest)
        with self.assertRaises(BindingError):
            self.store.finish_attempt(manifest, attempt, "finished", "foreign", "stale")
        self.assertEqual(str(self.store.get(manifest)["attempt_id"]), next_attempt)

    def test_error_is_terminal_and_event_revision_is_contiguous(self):
        manifest = self.make()
        attempt = self.store.begin(manifest)
        self.store.finish_attempt(manifest, attempt, "error", None, "controlled failure")
        with self.assertRaises(BindingError):
            self.store.begin(manifest)
        events = [e for e in self.store.export()["ac_job_events"] if e["job_id"] == manifest["job_id"]]
        self.assertEqual(sorted(e["revision"] for e in events), [0, 1, 2])

    def test_missing_checkpoint_never_returns_empty_success(self):
        with self.assertRaises(BindingError):
            read_snapshot(self.pg.dsn, self.make())

    def test_readonly_connection_cannot_write(self):
        with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
            with self.store.connect(readonly=True) as conn:
                conn.execute("DELETE FROM ac_jobs")

    def test_snapshot_with_foreign_identity_is_rejected_even_with_real_checkpoint(self):
        with Experiment(Path(self.temp.name), self.store) as exp:
            self.assertEqual(exp.call("start")["result"]["status"], "PASS")
            with checkpointer(self.pg.dsn) as saver:
                graph = build(saver, exp.manifest)
                snapshot = graph.get_state(config(exp.manifest))
                corrupted = snapshot._replace(values={**snapshot.values, "environment_id": str(uuid4())})
                with self.assertRaises(BindingError):
                    validate_snapshot(corrupted, exp.manifest)

    def test_coordinator_pointer_mismatch_refuses_resume(self):
        with Experiment(Path(self.temp.name), self.store) as exp:
            self.assertEqual(exp.call("start")["result"]["status"], "PASS")
            with self.store.connect() as conn:
                conn.execute("UPDATE ac_jobs SET checkpoint_id='foreign' WHERE job_id=%s", (exp.manifest["job_id"],))
            with self.assertRaises(BindingError):
                read_snapshot(self.pg.dsn, exp.manifest)


if __name__ == "__main__":
    unittest.main()

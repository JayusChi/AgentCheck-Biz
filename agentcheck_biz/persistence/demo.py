"""Real PG + HTTP + finite Agent processes: save, inspect, resume, refuse gaps."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.adapters.ticket_http import TicketHttpEnvironment
from agentcheck_biz.adapters.http_transport import HttpTimeouts
from agentcheck_biz.checks import load_json, observe_database
from agentcheck_biz.events import EventLog
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.reports import save_json
from .runtime import PostgresRuntime, child_environment
from .store import Store, new_manifest, digest, package_versions


def agent(manifest_path, output, dsn, action, *, service=None, gap=False):
    output = Path(output)
    log_path = output.with_suffix(".log")
    env = child_environment(dict(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1"))
    executable = sys.executable
    if os.name == "nt":
        executable = sys._base_executable
        env["__PYVENV_LAUNCHER__"] = sys.executable
    args = [executable, "-X", "utf8", "-m", "agentcheck_biz.persistence.worker", "--manifest", str(manifest_path),
            "--output", str(output), "--action", action] + (["--gap"] if gap else [])
    request = dict(dsn=dsn)
    if service:
        request.update(origin=service.transport.origin, token=service.tokens["tenant-A"])
    with log_path.open("wb") as stream:
        process = subprocess.Popen(args, cwd=REPO_ROOT, env=env, stdin=subprocess.PIPE,
            stdout=stream, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            process.communicate((json.dumps(request) + "\n").encode(), timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    result_path = output / "result.json"
    result = load_json(result_path) if result_path.is_file() else {"status": "ERROR", "reason": "Agent did not produce a result"}
    return dict(pid=process.pid, parent_pid=os.getpid(), exited=process.poll() is not None,
                exit_code=process.returncode, result_path=str(result_path), result=result)


class Experiment:
    def __init__(self, root, store):
        self.directory = Path(root).resolve() / ("checkpoint-" + uuid4().hex)
        self.directory.mkdir(parents=True)
        case = load_json(REPO_ROOT / "cases/tickets/full/T01.json")
        self.manifest = new_manifest(self.directory, case)
        self.manifest_path = self.directory / "manifest.json"
        save_json(self.manifest_path, self.manifest)
        store.create(self.manifest)
        self.store = store
        self.context = RunContext(self.directory.name, self.manifest["operation_id"], "environment-" + uuid4().hex,
                                  time.time() + 600, self.directory)
        self.events = EventLog(self.directory / "events.jsonl", self.context.run_id)
        self.service = TicketHttpEnvironment(case, "fixed", HttpTimeouts())
        self.steps = []

    def __enter__(self):
        try:
            self.service.prepare(self.context, self.events)
            self.initial = observe_database(self.directory / "business.sqlite")
            save_json(self.directory / "initial.json", self.initial)
            return self
        except BaseException:
            self.service.cleanup(self.context, self.events)
            raise

    def call(self, action, *, label=None, manifest=None, gap=False):
        name = label or action
        manifest_path = self.manifest_path
        if manifest is not None:
            manifest_path = self.directory / (name + "-manifest.json")
            save_json(manifest_path, manifest)
        item = agent(manifest_path, self.directory / name, self.store.dsn, action, service=self.service, gap=gap)
        self.steps.append(dict(label=name, **item))
        save_json(self.directory / "processes.json", self.steps)
        return item

    def __exit__(self, *args):
        try:
            self.final = observe_database(self.directory / "business.sqlite")
            save_json(self.directory / "final.json", self.final)
        finally:
            self.service.cleanup(self.context, self.events)


def suite(output):
    directory = Path(output).resolve() / ("d26-checkpoint-" + uuid4().hex)
    directory.mkdir(parents=True)
    report = dict(status="RUNNING", model_requests=0, implementation_sha256=implementation_digest(),
        packages=package_versions(), assertions={}, summary_path=str(directory / "summary.json"))
    save_json(directory / "summary.json", report)
    pg = PostgresRuntime(directory / "postgres")
    try:
        with pg:
            store = Store(pg.dsn)
            store.setup()
            store.setup()
            report["postgres"] = pg.identity
            with Experiment(directory, store) as normal:
                first = normal.call("start")
                assert first["result"]["status"] == "PASS" and first["result"]["job_state"] == "interrupted", first
                assert first["exited"] and first["exit_code"] == 0
                before = store.export()
                read = normal.call("read")
                after = store.export()
                assert read["result"]["status"] == "PASS" and read["pid"] != first["pid"]
                assert read["result"]["values"] == first["result"]["values"] and before == after
                report["assertions"]["new_process_reads_same_thread_without_writes"] = True
                pg.close()
                unavailable = normal.call("read", label="database_unavailable")
                assert unavailable["result"]["status"] == "ERROR" and unavailable["exit_code"] == 3
                pg.restart()
                assert pg.identity["system_identifier"] == report["postgres"]["system_identifier"]
                assert store.export() == before
                restarted_read = normal.call("read", label="after_postgres_restart")
                assert restarted_read["result"]["checkpoint_id"] == read["result"]["checkpoint_id"]
                report["assertions"]["database_unavailable_refused_and_cluster_restart_preserved_state"] = True
                with Experiment(directory, store) as foreign:
                    forged = deepcopy(normal.manifest)
                    forged["thread_id"] = foreign.manifest["thread_id"]
                    rejected = normal.call("read", label="foreign_thread", manifest=forged)
                    empty = foreign.call("read", label="no_checkpoint")
                    assert rejected["result"]["status"] == empty["result"]["status"] == "ERROR"
                    report["assertions"]["foreign_and_missing_checkpoints_refused"] = True
                resumed = normal.call("resume")
                assert resumed["result"]["job_state"] == "finished" and resumed["result"]["values"]["phase"] == "completed"
                assert resumed["result"]["attempt_id"] != first["result"]["attempt_id"]
                assert resumed["result"]["values"]["create_calls"] == resumed["result"]["values"]["query_calls"] == 1
                terminal = normal.call("resume", label="terminal_resume")
                assert terminal["result"]["status"] == "ERROR"
                report["assertions"]["same_job_new_attempt_resumes_without_replaying_create"] = True
            effects = [row for row in normal.final["tickets"] if row["tenant_id"] == "tenant-A"]
            assert len(effects) == 1 and effects[0] == first["result"]["values"]["ticket"]
            report["normal"] = dict(run_dir=str(normal.directory), manifest=normal.manifest, steps=normal.steps, resource_count=1)
            report["foreign"] = dict(run_dir=str(foreign.directory), manifest=foreign.manifest, steps=foreign.steps)
            with Experiment(directory, store) as gap:
                failure = gap.call("start", gap=True)
                assert failure["result"]["status"] == "INCONCLUSIVE" and failure["result"]["job_state"] == "waiting_verification"
                read = gap.call("read")
                assert read["result"]["values"]["phase"] == "prepared" and read["result"]["values"]["ticket"] is None
                denied = gap.call("resume")
                assert denied["result"]["status"] == "ERROR"
            assert len([r for r in gap.final["tickets"] if r["tenant_id"] == "tenant-A"]) == 1
            report["assertions"]["business_committed_without_checkpoint_never_blindly_replayed"] = True
            report["gap"] = dict(run_dir=str(gap.directory), manifest=gap.manifest, steps=gap.steps, resource_count=1)
            export = store.export()
            save_json(directory / "postgres-snapshot.json", export)
            report["database_snapshot_sha256"] = digest(export)
            report["job_states"] = {row["state"]: sum(j["state"] == row["state"] for j in export["ac_jobs"]) for row in export["ac_jobs"]}
            report["checkpoint_rows"] = len(export["checkpoints"])
            report["checkpoint_migration_versions"] = [row["v"] for row in export["checkpoint_migrations"]]
        report["cleanup"] = pg.receipts
        assert all(r["exited"] for r in pg.receipts)
        report["status"] = "PASS"
    except Exception as error:
        report.update(status="ERROR", error=type(error).__name__ + ": " + str(error))
    finally:
        save_json(directory / "summary.json", report)
    return report

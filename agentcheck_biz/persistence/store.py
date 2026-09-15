"""Versioned job identity and explicit transitions; no D27 lease scheduler yet."""

from contextlib import contextmanager
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from langgraph.checkpoint.postgres import PostgresSaver

from . import STORAGE_VERSION, GRAPH_VERSION

TRANSITIONS = {
    "queued": {"running", "error", "interrupted"},
    "running": {"waiting_verification", "finished", "error", "interrupted"},
    "waiting_verification": {"running", "error"},
    "interrupted": {"running", "waiting_verification", "error"},
    "finished": set(), "error": set(),
}
IDENTITY_KEYS = ("job_id", "experiment_id", "environment_id", "thread_id", "run_id", "operation_id", "evidence_dir", "request_sha256", "storage_version", "graph_version")
PINS = {"langgraph": "1.2.11", "langgraph-checkpoint": "4.2.0", "langgraph-checkpoint-postgres": "3.1.2", "psycopg": "3.3.5", "psycopg-pool": "3.3.1"}


class BindingError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def package_versions():
    values = {name: version(name) for name in PINS}
    if values != PINS:
        raise BindingError("Checkpoint dependency versions differ from the tested storage contract")
    return values


def new_manifest(evidence_dir, case):
    directory = Path(evidence_dir).resolve()
    return dict(job_id=str(uuid4()), experiment_id=str(uuid4()), environment_id=str(uuid4()), thread_id=str(uuid4()),
        run_id=directory.name, operation_id=case["context"]["operation_id"], evidence_dir=str(directory),
        request_sha256=digest(case), case=case, storage_version=STORAGE_VERSION, graph_version=GRAPH_VERSION,
        packages=package_versions(), checkpoint_namespace="", model_requests=0)


def validate_manifest(manifest):
    for key in ("job_id", "experiment_id", "environment_id", "thread_id"):
        if str(UUID(manifest[key])) != manifest[key]:
            raise BindingError("Invalid canonical UUID: " + key)
    if (manifest["storage_version"] != STORAGE_VERSION or manifest["graph_version"] != GRAPH_VERSION
            or manifest["packages"] != package_versions() or manifest["checkpoint_namespace"] != ""
            or manifest["request_sha256"] != digest(manifest["case"])
            or manifest["operation_id"] != manifest["case"]["context"]["operation_id"]
            or manifest["run_id"] != Path(manifest["evidence_dir"]).name or manifest["model_requests"] != 0):
        raise BindingError("Manifest version, request or environment binding mismatch")


class Store:
    def __init__(self, dsn):
        self.dsn = dsn

    @contextmanager
    def connect(self, *, readonly=False):
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            if readonly:
                conn.execute("SET TRANSACTION READ ONLY")
            yield conn

    def setup(self):
        script = Path(__file__).with_name("migrations") / "001_jobs.sql"
        checksum = hashlib.sha256(script.read_bytes()).hexdigest()
        # Shared lock covers both our migration and official saver migrations.
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row, prepare_threshold=0) as conn:
            conn.execute("SELECT pg_advisory_lock(262601)")
            try:
                with conn.transaction():
                    conn.execute("CREATE TABLE IF NOT EXISTS ac_schema_versions(version integer PRIMARY KEY, sha256 text NOT NULL)")
                    versions = conn.execute("SELECT * FROM ac_schema_versions ORDER BY version").fetchall()
                    if versions and versions != [{"version": 1, "sha256": checksum}]:
                        raise BindingError("Unsupported or modified coordinator migration")
                    if not versions:
                        conn.execute(script.read_text(encoding="utf8"), prepare=False)
                        conn.execute("INSERT INTO ac_schema_versions VALUES (1,%s)", (checksum,))
                saver = PostgresSaver(conn)
                existing = conn.execute("SELECT to_regclass('checkpoint_migrations') AS name").fetchone()["name"]
                if existing:
                    maximum = conn.execute("SELECT max(v) AS v FROM checkpoint_migrations").fetchone()["v"]
                    if maximum is not None and maximum >= len(saver.MIGRATIONS):
                        raise BindingError("Checkpoint schema is newer than the pinned adapter")
                saver.setup()
            finally:
                conn.execute("SELECT pg_advisory_unlock(262601)")

    def create(self, manifest):
        validate_manifest(manifest)
        with self.connect() as conn:
            conn.execute("""INSERT INTO ac_jobs(job_id,experiment_id,environment_id,thread_id,run_id,operation_id,
                evidence_dir,request_sha256,manifest,state,storage_version,graph_version)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s)""",
                tuple(manifest[k] for k in IDENTITY_KEYS[:8]) + (Jsonb(manifest), STORAGE_VERSION, GRAPH_VERSION))
            conn.execute("INSERT INTO ac_job_events(job_id,state,revision,detail) VALUES(%s,'queued',0,%s)",
                         (manifest["job_id"], Jsonb({"reason": "created"})))
        return self.get(manifest)

    def bound(self, conn, manifest, *, lock=False):
        validate_manifest(manifest)
        row = conn.execute("SELECT * FROM ac_jobs WHERE job_id=%s" + (" FOR UPDATE" if lock else ""), (manifest["job_id"],)).fetchone()
        if row is None or row["manifest"] != manifest or any(str(row[k]) != str(manifest[k]) for k in IDENTITY_KEYS):
            raise BindingError("Job, experiment, environment, thread or request does not match persisted identity")
        return row

    def get(self, manifest):
        with self.connect(readonly=True) as conn:
            return self.bound(conn, manifest)

    def _transition(self, conn, row, state, detail, checkpoint_id=None):
        if state not in TRANSITIONS[row["state"]]:
            raise BindingError(f"Illegal job transition: {row['state']} -> {state}")
        revision = row["revision"] + 1
        conn.execute("UPDATE ac_jobs SET state=%s,revision=%s,checkpoint_id=COALESCE(%s,checkpoint_id),updated_at=now() WHERE job_id=%s",
                     (state, revision, checkpoint_id, row["job_id"]))
        conn.execute("INSERT INTO ac_job_events(job_id,attempt_id,previous_state,state,revision,detail) VALUES(%s,%s,%s,%s,%s,%s)",
            (row["job_id"], row["attempt_id"], row["state"], state, revision, Jsonb(detail)))

    def begin(self, manifest):
        with self.connect() as conn:
            row = self.bound(conn, manifest, lock=True)
            attempt = str(uuid4())
            ordinal = conn.execute("SELECT count(*) AS n FROM ac_attempts WHERE job_id=%s", (row["job_id"],)).fetchone()["n"] + 1
            conn.execute("INSERT INTO ac_attempts(attempt_id,job_id,ordinal,pid) VALUES(%s,%s,%s,%s)", (attempt, row["job_id"], ordinal, os.getpid()))
            conn.execute("UPDATE ac_jobs SET attempt_id=%s WHERE job_id=%s", (attempt, row["job_id"]))
            row["attempt_id"] = attempt
            self._transition(conn, row, "running", {"reason": "explicit_attempt", "pid": os.getpid()})
        return attempt

    def finish_attempt(self, manifest, attempt, state, checkpoint_id, reason):
        with self.connect() as conn:
            row = self.bound(conn, manifest, lock=True)
            if str(row["attempt_id"]) != attempt:
                raise BindingError("Stale attempt cannot update job status")
            self._transition(conn, row, state, {"reason": reason, "pid": os.getpid()}, checkpoint_id)

    def export(self):
        from psycopg import sql
        with self.connect(readonly=True) as conn:
            tables = [r["tablename"] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
            return {name: [row["row"] for row in conn.execute(sql.SQL("SELECT to_jsonb(t) AS row FROM {} t ORDER BY to_jsonb(t)::text").format(sql.Identifier(name)))] for name in tables}

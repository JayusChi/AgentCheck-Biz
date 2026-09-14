"""Own an isolated loopback PostgreSQL cluster; never attach to a user's server."""

import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import secrets
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
from uuid import uuid4
import zipfile

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg import sql

from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json

VERSION = "17.11"
PACKAGE_VERSION = "17.11.0"
PLATFORM = "windows" if os.name == "nt" else "linux"
ARCHIVE_SHA256 = {"windows": "98040fae18dd9633ff95932125b0cecf0a45a1a9312e216e2a33ad03a31d4251",
                  "linux": "0dd7b72b6f335b8ecfb355fa24c5781e8a93edd09880bb77eb52ebbf29b3e96d"}[PLATFORM]
DEFAULT_BINARY = REPO_ROOT / ".cache/postgres" / PACKAGE_VERSION / "runtime/bin" / ("postgres.exe" if os.name == "nt" else "postgres")


def child_environment(extra=None):
    keep = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "SYSTEMDRIVE", "PATHEXT"}
    return {**{k: v for k, v in os.environ.items() if k.upper() in keep}, **(extra or {})}


def extract_native(native, destination):
    """Materialize in-archive library links as files; never create filesystem links."""
    destination = Path(destination).absolute()
    if destination.exists() and (destination.is_symlink() or getattr(destination.lstat(),'st_file_attributes',0) & 0x400):
        raise ValueError('Linked PostgreSQL destination refused')
    # Hosted Windows uses a short (8.3) TEMP path; compare canonical paths.
    destination = destination.resolve()
    members = {m.name.rstrip('/'):m for m in native.getmembers()}
    if len(members) != len(native.getmembers()):
        raise ValueError('Duplicate PostgreSQL archive member')
    resolved = []
    for name, member in members.items():
        target = destination / name
        if (name.startswith('/') or '\\' in name or '..' in name.split('/')
                or destination not in target.resolve().parents or target.is_symlink()):
            raise ValueError('Unsafe PostgreSQL archive path')
        source, seen = member, set()
        while source.issym() or source.islnk():
            if source.name in seen or source.linkname.startswith('/') or '\\' in source.linkname:
                raise ValueError('Unsafe PostgreSQL library link')
            seen.add(source.name)
            linked = posixpath.normpath(posixpath.join(posixpath.dirname(source.name) if source.issym() else '',source.linkname))
            if linked not in members or linked.startswith('../'):
                raise ValueError('PostgreSQL library link escapes package')
            source = members[linked]
        if not source.isfile() and not (source.isdir() and source is member):
            raise ValueError('Unsupported PostgreSQL archive member')
        resolved.append((target,source))
    for target, source in resolved:
        if source.isdir():
            target.mkdir(parents=True,exist_ok=True)
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            with native.extractfile(source) as incoming, target.open('wb') as outgoing:
                shutil.copyfileobj(incoming,outgoing)
            if os.name != 'nt': target.chmod(source.mode & 0o777)


class PostgresRuntime:
    def __init__(self, root, binary=DEFAULT_BINARY):
        self.binary = Path(binary).resolve()
        self.instance_id = "pg-" + uuid4().hex
        self.directory = Path(root).resolve() / self.instance_id
        # Windows initdb/libpq interpret native paths using the ANSI code page.
        # Keep native binaries and PGDATA ASCII; project evidence may be Unicode.
        self.execution_root = Path(tempfile.gettempdir()).resolve() / ("agentcheck-" + self.instance_id)
        self.data = self.execution_root / "data"
        self.owner_pid = os.getpid()
        self.process = self.log = None
        self.password = secrets.token_urlsafe(32)
        self.database = "ac_" + uuid4().hex
        self.generation = 0
        self.receipts = []

    def command(self, executable, args, timeout=40):
        if os.name != "nt":
            executable = executable.removesuffix(".exe")
        result = subprocess.run([str(self.binary.parent / executable), *args], cwd=self.directory,
            env=child_environment(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            (self.directory / (executable + "-error.log")).write_bytes(result.stdout)
            raise RuntimeError(executable + " failed; inspect the isolated runtime log")
        return result.stdout.decode("utf8", "replace")

    def start(self):
        if not self.binary.is_file():
            raise FileNotFoundError("Install the pinned PostgreSQL package with scripts/fetch_postgres.py")
        self.directory.mkdir(parents=True, exist_ok=False)
        self.execution_root.mkdir(parents=True, exist_ok=False)
        if os.name == "nt":
            if not str(self.execution_root).isascii():
                raise ValueError("PostgreSQL requires an ASCII temporary directory on this Windows host")
            archive = DEFAULT_BINARY.parents[2] / "postgres-binaries.jar"
            if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
                raise ValueError("PostgreSQL archive differs from its pinned checksum")
            with zipfile.ZipFile(archive) as package, tarfile.open(fileobj=io.BytesIO(package.read("postgres-windows-x86_64.txz")), mode="r:xz") as native:
                destination = self.execution_root / "runtime"
                extract_native(native,destination)
            self.binary = destination / "bin/postgres.exe"
        if self.command("postgres.exe", ["--version"]).strip() != "postgres (PostgreSQL) " + VERSION:
            raise ValueError("Unexpected PostgreSQL version")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        password_file = self.execution_root / "bootstrap-password"
        password_file.write_text(self.password + "\n", encoding="utf8")
        try:
            self.command("initdb.exe", ["-D", str(self.data), "-U", "ac_test", "--encoding=UTF8", "--locale=C",
                "--auth-host=scram-sha-256", "--auth-local=scram-sha-256", "--pwfile=" + str(password_file)])
        finally:
            password_file.unlink(missing_ok=True)
        self.admin_dsn = make_conninfo(host="127.0.0.1", port=self.port, dbname="postgres", user="ac_test",
            password=self.password, connect_timeout=3, options="-c statement_timeout=5000 -c lock_timeout=3000")
        self.dsn = make_conninfo(self.admin_dsn, dbname=self.database)
        try:
            self.restart()
            with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
                conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(self.database)))
            return self
        except BaseException:
            self.close()
            raise

    def restart(self):
        if self.owner_pid != os.getpid() or (self.process is not None and self.process.poll() is None):
            raise RuntimeError("Restart requires an exited owned PostgreSQL process")
        self.generation += 1
        self.log = (self.directory / f"postgres-{self.generation}.log").open("wb")
        self.process = subprocess.Popen([str(self.binary), "-D", str(self.data), "-h", "127.0.0.1", "-p", str(self.port),
            "-c", "max_connections=20", "-c", "shared_buffers=32MB", "-c", "fsync=on"], cwd=self.directory,
            env=child_environment(), stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 20
        while True:
            if self.process.poll() is not None:
                raise RuntimeError("PostgreSQL exited during startup")
            try:
                with psycopg.connect(self.admin_dsn) as conn:
                    version, data = conn.execute("SELECT current_setting('server_version'), current_setting('data_directory')").fetchone()
                    system_id = conn.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise TimeoutError("PostgreSQL startup deadline")
                time.sleep(.05)
        pid = int((self.data / "postmaster.pid").read_text().splitlines()[0])
        if pid != self.process.pid or Path(data).resolve() != self.data or not version.startswith(VERSION):
            raise RuntimeError("Owned PostgreSQL identity mismatch")
        self.identity = dict(instance_id=self.instance_id, generation=self.generation, pid=pid, owner_pid=self.owner_pid,
            host="127.0.0.1", port=self.port, database=self.database, data_directory=str(self.data),
            version=version, system_identifier=system_id, package_version=PACKAGE_VERSION,
            binary_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest(), test_only=True)
        save_json(self.directory / f"ready-{self.generation}.json", self.identity)

    def close(self):
        if self.process is not None and self.process.poll() is None:
            if self.owner_pid != os.getpid():
                raise RuntimeError("Cannot stop another owner's cluster")
            pid = int((self.data / "postmaster.pid").read_text().splitlines()[0])
            if pid != self.process.pid:
                raise RuntimeError("Cluster PID does not match the owned handle")
            self.command("pg_ctl.exe", ["-D", str(self.data), "stop", "-m", "fast", "-w", "-t", "10"], timeout=15)
            self.process.wait(timeout=5)
        if self.process is not None:
            receipt = dict(instance_id=self.instance_id, generation=self.generation, pid=self.process.pid,
                owner_pid=self.owner_pid, exited=self.process.poll() is not None, returncode=self.process.returncode)
            if not self.receipts or self.receipts[-1]["generation"] != self.generation:
                self.receipts.append(receipt)
            save_json(self.directory / "cleanup.json", dict(status="PASS", processes=self.receipts))
        if self.log is not None:
            self.log.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()

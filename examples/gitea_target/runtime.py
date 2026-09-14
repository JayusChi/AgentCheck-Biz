"""Own one isolated native Gitea process; credentials are never persisted here."""

import hashlib
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from uuid import uuid4

import httpx

from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json

VERSION = "1.26.4"
BINARY_SHA256 = ("7ce663c56143843f17b297f103d55c29913eb682998b91fec1e57d0921110917" if os.name == "nt"
                 else "0faa36d151918f8f7d6e0f3ae67597d1c338583d695add146ac393109d0fc44a")
DEFAULT_BINARY = REPO_ROOT / ".cache/gitea" / VERSION / ("gitea.exe" if os.name == "nt" else "gitea")
DEFAULT_INSTANCE_ROOT = REPO_ROOT / "artifacts/v2/gitea-instances"
TOKEN_SCOPES = {"GITEA_MANAGEMENT_TOKEN": "write:repository,write:issue,write:user",
                "GITEA_EXECUTION_TOKEN": "write:issue", "GITEA_OBSERVER_TOKEN": "read:repository,read:issue"}


class GiteaRuntime:
    def __init__(self, root, binary=DEFAULT_BINARY):
        self.binary = Path(binary).resolve()
        self.instance_id = "gitea-" + uuid4().hex
        self.directory = Path(root).resolve() / self.instance_id
        self.owner = "ac" + uuid4().hex[:16]
        self.process = self.log = None
        self.credentials = {}
        self.owner_pid = os.getpid()

    def command(self, *args, secret=False):
        result = subprocess.run([str(self.binary), "--work-path", str(self.directory), "--config", str(self.config), *args],
            cwd=self.directory, env=self.child_env, capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            # CLI user/token output can contain passwords. Never log that output.
            if not secret:
                (self.directory / "command-error.log").write_bytes(result.stdout + result.stderr)
            raise RuntimeError("Gitea setup command failed" + (" (sensitive output withheld)" if secret else "; see command-error.log"))
        return result.stdout.decode("utf-8")

    def start(self):
        if os.name == "nt" and len(str(self.directory)) > 140:
            raise ValueError("Windows Gitea requires a short instance root; keep deep API evidence in a separate directory")
        if not self.binary.is_file():
            raise FileNotFoundError("Install the pinned official binary with scripts/fetch_gitea.py")
        if hashlib.sha256(self.binary.read_bytes()).hexdigest() != BINARY_SHA256:
            raise ValueError("Gitea binary does not match the pinned official checksum")
        self.directory.mkdir(parents=True, exist_ok=False)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.config = self.directory / "app.ini"
        self.config.write_text(f"""APP_NAME = AgentCheck {self.instance_id}
RUN_MODE = prod
[server]
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {self.port}
ROOT_URL = {self.origin}/
LOCAL_ROOT_URL = {self.origin}/
APP_DATA_PATH = {self.directory.as_posix()}/data
DISABLE_SSH = true
OFFLINE_MODE = true
DISABLE_HTTP_GIT = true
LFS_START_SERVER = false
[database]
DB_TYPE = sqlite3
PATH = {self.directory.as_posix()}/data/gitea.db
[security]
INSTALL_LOCK = true
[service]
DISABLE_REGISTRATION = true
REQUIRE_SIGNIN_VIEW = true
ENABLE_NOTIFY_MAIL = false
[repository]
ROOT = {self.directory.as_posix()}/data/repositories
[actions]
ENABLED = false
[packages]
ENABLED = false
[mailer]
ENABLED = false
[cron]
ENABLED = false
[log]
MODE = console
LEVEL = Info
[api]
ENABLE_SWAGGER = true
MAX_RESPONSE_ITEMS = 50
[oauth2]
ENABLED = false
""", encoding="utf-8")
        keep = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "SYSTEMDRIVE", "PATHEXT", "USERNAME"}
        self.child_env = {key: value for key, value in os.environ.items() if key.upper() in keep}
        # Native Git must support deep evidence directories and must never walk
        # upward into the user's checkout if a target repository is unavailable.
        (self.directory / "gitconfig").write_text("[core]\n\tlongpaths = true\n", encoding="utf-8")
        self.child_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(self.directory / "gitconfig"),
                              GIT_CEILING_DIRECTORIES=str(self.directory), USERPROFILE=str(self.directory))
        self.log = (self.directory / "server.log").open("wb")
        pidfile = self.directory / "gitea.pid"
        self.process = subprocess.Popen([str(self.binary), "--work-path", str(self.directory), "--config", str(self.config),
            "web", "--pid", str(pidfile)], cwd=self.directory, env=self.child_env,
            stdout=self.log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 40
            with httpx.Client(base_url=self.origin, timeout=2, trust_env=False, follow_redirects=False) as client:
                while True:
                    if self.process.poll() is not None:
                        raise RuntimeError("Gitea exited during startup; see isolated server.log")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Gitea readiness timeout")
                    try:
                        response = client.get("/api/v1/version")
                        # REQUIRE_SIGNIN_VIEW also protects the version API.
                        # Wait for routing, provision identities, then verify it with auth.
                        if response.status_code in {200, 403} and pidfile.exists():
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                if int(pidfile.read_text().strip()) != self.process.pid:
                    raise RuntimeError("Gitea PID file does not match the owned process")
                self.command("admin", "user", "create", "--username", "bootstrap-admin", "--email", "bootstrap@example.invalid",
                             "--random-password", "--admin", "--must-change-password=false", secret=True)
                self.command("admin", "user", "create", "--username", self.owner, "--email", self.owner + "@example.invalid",
                             "--random-password", "--must-change-password=false", secret=True)
                for variable, scopes in TOKEN_SCOPES.items():
                    output = self.command("admin", "user", "generate-access-token", "--username", self.owner,
                                          "--token-name", variable.lower(), "--scopes", scopes, "--raw", secret=True)
                    tokens = re.findall(r"^[0-9a-f]{40}$", output, re.MULTILINE)
                    if len(tokens) != 1:
                        raise RuntimeError("Unexpected Gitea token output (withheld)")
                    self.credentials[variable] = tokens[0]
                response = client.get("/api/v1/user", headers={"Authorization": "token " + self.credentials["GITEA_MANAGEMENT_TOKEN"]})
                response.raise_for_status()
                user = response.json()
                if user["login"] != self.owner or user["is_admin"]:
                    raise RuntimeError("Dedicated non-admin Gitea identity was not verified")
                headers = {"Authorization": "token " + self.credentials["GITEA_MANAGEMENT_TOKEN"]}
                version = client.get("/api/v1/version", headers=headers)
                if version.status_code != 200 or version.json() != {"version": VERSION}:
                    raise RuntimeError("Authenticated Gitea version mismatch")
                schema = client.get("/swagger.v1.json", headers=headers)
                schema.raise_for_status()
                (self.directory / "swagger.v1.json").write_bytes(schema.content)
                self.identity = {"instance_id": self.instance_id, "origin": self.origin, "owner": self.owner,
                    "pid": self.process.pid, "owner_pid": self.owner_pid, "version": VERSION,
                    "binary_sha256": BINARY_SHA256, "deployment": "official-windows-binary" if os.name == "nt" else "official-linux-binary",
                    "swagger_sha256": hashlib.sha256(schema.content).hexdigest(), "test_only": True}
                save_json(self.directory / "instance.json", self.identity)
            return self
        except BaseException:
            self.close()
            raise

    def environment(self):
        return {**self.credentials, "GITEA_ORIGIN": self.origin, "GITEA_OWNER": self.owner,
                "GITEA_INSTANCE_ID": self.instance_id, "GITEA_EXPECTED_VERSION": VERSION,
                "GITEA_TEST_ONLY": "1"}

    def close(self):
        if self.process is not None:
            if os.getpid() != self.owner_pid:
                raise RuntimeError("Cannot clean up another owner's Gitea process")
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            save_json(self.directory / "cleanup.json", {"instance_id": self.instance_id, "pid": self.process.pid,
                "owner_pid": self.owner_pid, "exited": self.process.poll() is not None, "returncode": self.process.returncode})
        if self.log is not None:
            self.log.close()
        self.credentials.clear()

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()

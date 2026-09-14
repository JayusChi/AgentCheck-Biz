"""Bounded owned Python child with continuously preserved disk output."""

import os
from pathlib import Path
import subprocess
import sys

from agentcheck_biz.provenance import REPO_ROOT


def launch(args, log, timeout=1200):
    keep = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "SYSTEMDRIVE", "PATHEXT", "USERNAME",
            "VIRTUAL_ENV", "PYTHONUTF8", "PYTHONIOENCODING"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    executable = sys.executable
    if sys.platform == "win32":
        executable = sys._base_executable
        env["__PYVENV_LAUNCHER__"] = sys.executable
    path = Path(log)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        process = subprocess.Popen([executable, "-X", "utf8", *args], cwd=REPO_ROOT, env=env,
            stdout=stream, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            stream.write(b"\nOwned command exceeded deadline; partial evidence retained.\n")
            code = 124
    return dict(command=args, pid=process.pid, exit_code=code, exited=process.poll() is not None,
                timeout_seconds=timeout, log=str(path))

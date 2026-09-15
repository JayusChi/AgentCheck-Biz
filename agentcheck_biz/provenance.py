"""Source identity includes nested adapters, observers and verifiers."""
import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def implementation_digest() -> str:
    digest = hashlib.sha256()
    for folder in (REPO_ROOT / "agentcheck_biz", REPO_ROOT / "examples" / "ticket_agent",
                   REPO_ROOT / "examples" / "ticket_http", REPO_ROOT / "examples" / "gitea_target",
                   REPO_ROOT / "examples" / "business_mcp"):
        for path in sorted(folder.rglob("*.py")):
            digest.update(path.relative_to(REPO_ROOT).as_posix().encode("utf-8"))
            digest.update(b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()

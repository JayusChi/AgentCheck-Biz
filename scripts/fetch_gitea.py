"""Download and checksum the pinned official Windows target; never executes it."""

import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from examples.gitea_target.runtime import VERSION, BINARY_SHA256, DEFAULT_BINARY


def main():
    if DEFAULT_BINARY.is_file() and hashlib.sha256(DEFAULT_BINARY.read_bytes()).hexdigest() == BINARY_SHA256:
        print("Pinned Gitea binary already present and verified")
        return
    suffix = "windows-4.0-amd64.exe" if os.name == "nt" else "linux-amd64"
    url = f"https://dl.gitea.com/gitea/{VERSION}/gitea-{VERSION}-{suffix}"
    directory = DEFAULT_BINARY.parent
    directory.mkdir(parents=True, exist_ok=True)
    compressed = directory / "download.xz"
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        checksum = client.get(url + ".xz.sha256")
        checksum.raise_for_status()
        expected = checksum.text.split()[0]
        digest = hashlib.sha256()
        started = time.monotonic()
        with client.stream("GET", url + ".xz") as response, compressed.open("wb") as output:
            response.raise_for_status()
            for chunk in response.iter_bytes(1024 * 1024):
                if time.monotonic() - started > 240:
                    raise TimeoutError("Gitea download deadline exceeded")
                output.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("Compressed Gitea checksum mismatch")
    candidate = directory / "verified-candidate.exe"
    with lzma.open(compressed, "rb") as source, candidate.open("wb") as output:
        shutil.copyfileobj(source, output)
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != BINARY_SHA256:
        raise ValueError("Gitea executable checksum mismatch")
    candidate.replace(DEFAULT_BINARY)
    if os.name != "nt":
        DEFAULT_BINARY.chmod(0o755)
    print(json.dumps({"path": str(DEFAULT_BINARY), "version": VERSION, "sha256": BINARY_SHA256}))


if __name__ == "__main__":
    main()

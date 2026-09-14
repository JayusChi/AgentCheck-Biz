"""Fetch the pinned Zonky-packaged PostgreSQL Windows binaries; verify before use."""

import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import time
import zipfile

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentcheck_biz.persistence.runtime import ARCHIVE_SHA256, PACKAGE_VERSION, DEFAULT_BINARY, PLATFORM, extract_native


def main():
    relative = f"io/zonky/test/postgres/embedded-postgres-binaries-{PLATFORM}-amd64/{PACKAGE_VERSION}/embedded-postgres-binaries-{PLATFORM}-amd64-{PACKAGE_VERSION}.jar"
    directory = DEFAULT_BINARY.parents[2]
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "postgres-binaries.jar"
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        errors = []
        for base in ("https://repo.maven.apache.org/maven2/", "https://repo.huaweicloud.com/repository/maven/"):
            try:
                candidate = directory / "download.partial"
                started = time.monotonic()
                with httpx.Client(timeout=20, follow_redirects=True) as client, client.stream("GET", base + relative) as response, candidate.open("wb") as output:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        if time.monotonic() - started > 240:
                            raise TimeoutError("PostgreSQL package download deadline")
                        output.write(chunk)
                if hashlib.sha256(candidate.read_bytes()).hexdigest() != ARCHIVE_SHA256:
                    raise ValueError("PostgreSQL archive checksum mismatch")
                candidate.replace(archive)
                break
            except Exception as error:
                errors.append(type(error).__name__)
        else:
            raise RuntimeError("Cannot fetch verified PostgreSQL package: " + ", ".join(errors))
    destination = directory / "runtime"
    with zipfile.ZipFile(archive) as package, tarfile.open(fileobj=io.BytesIO(package.read(f"postgres-{PLATFORM}-x86_64.txz")), mode="r:xz") as native:
        extract_native(native,destination)
    print(json.dumps(dict(package_version=PACKAGE_VERSION, sha256=ARCHIVE_SHA256, binary=str(DEFAULT_BINARY))))


if __name__ == "__main__":
    main()

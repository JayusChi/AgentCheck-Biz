"""Create a fresh verified source checkout, venv, native runtimes and D35 demo."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentcheck_biz.ci.artifacts import source_bundle
from agentcheck_biz.delivery import owned_path, verify_bundle, save
from verify_v2 import clean_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--binary-cache', action='store_true', help='Copy only checksum-verified official PG archive and Gitea executable; no databases')
    args = parser.parse_args()
    output = owned_path(args.directory)
    output.mkdir(parents=True, exist_ok=False)
    report = dict(schema='d35-fresh/1', status='INCONCLUSIVE', steps=[], model_requests=0,
                  new_venv=True, reused_runtime_data=False, binary_cache=args.binary_cache)
    environment = clean_environment()
    def step(name, command, cwd, timeout=600):
        print(name + ' starting', flush=True)
        started = time.monotonic()
        with (output / (name + '.log')).open('wb') as log:
            process = subprocess.run([str(x) for x in command], cwd=cwd, env=environment, stdout=log,
                stderr=subprocess.STDOUT, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        report['steps'].append(dict(name=name, exit_code=process.returncode, seconds=round(time.monotonic() - started, 2)))
        save(output / 'fresh.json', report)
        if process.returncode:
            raise RuntimeError(name + ' failed; see retained log')
        print(name + ' PASS', flush=True)
    try:
        report['bundle'] = source_bundle(ROOT, output / 'agentcheck-v2-source.zip')
        verify_bundle(output / 'agentcheck-v2-source.zip')
        source = output / 'source'
        with zipfile.ZipFile(output / 'agentcheck-v2-source.zip') as archive:
            archive.extractall(source)  # All paths and bytes validated above.
        # Do not import unrelated global site/.pth customizations during bootstrap.
        step('new-venv', [sys._base_executable, '-I', '-S', '-m', 'venv', source / '.venv'], source)
        python = source / '.venv/Scripts/python.exe'
        step('frozen-python-install', [python, '-m', 'pip', 'install', '--disable-pip-version-check', '-r',
            'requirements-acceptance.txt', '-r', 'requirements-persistence.txt'], source, 1200)
        if args.binary_cache:
            from agentcheck_biz.persistence.runtime import DEFAULT_BINARY as pg, ARCHIVE_SHA256
            from examples.gitea_target.runtime import DEFAULT_BINARY as gitea, BINARY_SHA256
            for path, expected in ((pg.parents[2] / 'postgres-binaries.jar', ARCHIVE_SHA256), (gitea, BINARY_SHA256)):
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError('Cached official binary checksum mismatch')
                target = source / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        step('prepare-postgres', [python, 'scripts/fetch_postgres.py'], source)
        step('prepare-gitea', [python, 'scripts/fetch_gitea.py'], source)
        step('delivery-tests', [python, '-X', 'utf8', '-m', 'unittest', 'tests.v2.test_delivery', 'tests.v2.test_live_recovery', '-v'], source)
        step('delivery-demo', [python, '-X', 'utf8', 'scripts/delivery.py', 'demo', '--directory', 'artifacts/delivery'], source, 2400)
        report.update(status='PASS', source=str(source), demo='source/artifacts/delivery/delivery.json')
    except Exception as exc:
        report.update(status='ERROR', error=type(exc).__name__ + ': ' + str(exc))
    save(output / 'fresh.json', report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] == 'PASS' else 3


if __name__ == '__main__':
    raise SystemExit(main())

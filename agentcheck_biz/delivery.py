"""Reproducible local candidate delivery. No model credentials or paid execution."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import threading
import time
import urllib.request
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_SCENARIOS = ['committed-tool-result', 'storage-unavailable', 'gitea-resume', 'gitea-unknown-empty']


def read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.partial')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    temporary.replace(path)


def owned_path(path):
    from agentcheck_biz.ci.artifacts import reject_linked_directory
    path = Path(path).absolute()
    reject_linked_directory(path)
    path = path.resolve()
    if not path.is_relative_to(ROOT / 'artifacts') or path == ROOT / 'artifacts':
        raise ValueError('Delivery output must be inside this checkout artifacts directory')
    return path


def verify_bundle(path):
    """Verify all members before extraction; no missing/extra/link/escaping files."""
    from agentcheck_biz.ci.artifacts import FORBIDDEN, SECRET
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        records = json.loads(archive.read('SOURCE-SHA256.json'))
        expected = {r['path']: r['sha256'] for r in records}
        if not records or len(records) != len(expected) or len(names) != len(set(names)):
            raise ValueError('Empty or duplicate archive manifest')
        if set(names) != set(expected) | {'SOURCE-SHA256.json'}:
            raise ValueError('Archive differs from its source manifest')
        for name in names:
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith('/') or '\\' in name or ':' in name or '..' in parts
                    or any(p in FORBIDDEN or p.startswith('.env') for p in parts)
                    or archive.getinfo(name).external_attr >> 16 & 0o170000 == 0o120000):
                raise ValueError('Unsafe archive member')
            data = archive.read(name)
            if SECRET.search(data):
                raise ValueError('Credential-shaped archive content')
            if name in expected and hashlib.sha256(data).hexdigest() != expected[name]:
                raise ValueError('Source checksum mismatch')
    return dict(status='PASS', files=len(records), sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())


def recovery_index(ci_directory, output):
    """Bind a newly generated CI recovery suite after its independent recheck."""
    from agentcheck_biz.network_acceptance.verify import hashes
    ci_directory = owned_path(ci_directory)
    summary = read(ci_directory / 'public/summary.json')
    checked = read(ci_directory / 'private/integration/recovery-check/stage.json')
    candidates = list((ci_directory / 'private/integration/recovery').glob('*/summary.json'))
    if (summary['status'] != 'PASS' or summary['scope'] != 'all' or len(candidates) != 1
            or checked['status'] != 'PASS' or checked['experiments'] != 4):
        raise ValueError('Complete independently checked recovery evidence is required')
    path = candidates[0]
    suite = read(path)
    if suite['status'] != 'PASS' or {s['name'] for s in suite['scenarios']} != set(RECOVERY_SCENARIOS):
        raise ValueError('Recovery scenario coverage differs')
    index = dict(status='PASS', continuity_recheck=checked,
                 continuity_suite={'summary_path': str(path)}, continuity_evidence_sha256=hashes(path.parent))
    save(output, index)
    from dashboard.api.recovery import RecoveryEvidence
    view = RecoveryEvidence(output, ci_directory).list()
    if not view['ready'] or len(view['runs']) != 4:
        raise ValueError('Recovery page cannot read this evidence')
    return dict(status='PASS', runs=len(view['runs']), report=str(output))


def run_release_gate(directory, entry):
    """Preserve the verifier's FAIL/1; the self-test wrapper expects only zero."""
    from agentcheck_biz.network_acceptance.process import launch
    directory.mkdir(parents=True, exist_ok=False)
    request, result_path = directory / 'request.json', directory / 'independent-recheck.json'
    save(request, dict(controller_pid=os.getpid(), entries=[entry]))
    process = launch(['-m', 'agentcheck_biz.network_acceptance', '--worker', str(request), '--result', str(result_path)],
                     directory / 'verifier.log', timeout=60)
    result = read(result_path)
    expected_exit = {'PASS': 0, 'FAIL': 1}.get(result.get('status'))
    if (expected_exit is None or process['exit_code'] != expected_exit
            or result.get('worker_pid') != process['pid'] or process['pid'] == os.getpid()
            or result.get('controller_pid') != os.getpid()
            or result.get('planned') != 1 or result.get('executed') != 1):
        raise ValueError('Missing or inconsistent independent gate result')
    return result | {'step': process}


def demo(output):
    from agentcheck_biz.ci.worker import block_external
    from agentcheck_biz.network_acceptance.profiles import profiles
    from agentcheck_biz.network_acceptance.runner import execute, independent_check
    from agentcheck_biz.network_acceptance.verify import hashes, inspect_run
    output.mkdir(parents=True, exist_ok=False)
    report = dict(schema='d35-demo/1', status='INCONCLUSIVE', model_requests=0,
                  started_at=datetime.now(timezone.utc).isoformat(), steps=[])
    save(output / 'delivery.json', report)
    try:
        command = [sys.executable, '-X', 'utf8', str(ROOT / 'scripts/verify_v2.py'), '--output', str(output / 'ci')]
        result = subprocess.run(command, cwd=ROOT, check=False)
        ci = read(output / 'ci/public/summary.json')
        if result.returncode or ci['status'] != 'PASS':
            raise ValueError('CI verification failed; original report retained')
        report['steps'].append(dict(name='ci', status='PASS', report='ci/public/summary.json'))
        sys.addaudithook(block_external)
        known = {p['id']: p for p in profiles()}
        entries = []
        report['controls'] = []
        for name in ('A_unsafe_control', 'B_unsafe_loss_retry', 'C_fixed_loss_retry'):
            print('D35 control: ' + name, flush=True)
            row = execute(known[name], output / 'controls')
            directory = Path(row['run_dir'])
            entries.append(dict(id=name, kind='proxy', run_dir=str(directory), expected=known[name]['expected'],
                                require_fresh=True, evidence_sha256=hashes(directory)))
            report['controls'].append(dict(id=name, run_dir=str(directory), observed=inspect_run(directory, 'proxy')))
            save(output / 'delivery.json', report)
        audit = independent_check(output / 'controls', entries)
        if audit['status'] != 'PASS':
            raise ValueError('A/B/C evidence failed independent verification')
        # The unsafe candidate also faces the unchanged fixed release contract.
        negative = dict(entries[1], id='unsafe-release-gate', expected=known['C_fixed_loss_retry']['expected'])
        gate = run_release_gate(output / 'negative-gate', negative)
        if gate['status'] != 'FAIL' or gate['executed'] != 1 or gate['step']['exit_code'] != 1:
            raise ValueError('Known duplicate was not rejected by the release contract')
        report['steps'].extend([dict(name='abc-independent-check', status='PASS'),
            dict(name='known-defect-rejected', status='PASS', business_gate='FAIL', exit_code=gate['step']['exit_code'])])
        report['recovery'] = recovery_index(output / 'ci', output / 'recovery-index.json')
        report.update(status='PASS', ci=ci, independent_trial='PENDING_EXTERNAL_FEEDBACK',
                      publication='LOCAL_CANDIDATE', finished_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        report.update(status='ERROR', error=type(exc).__name__ + ': ' + str(exc))
        raise
    finally:
        save(output / 'delivery.json', report)
    return report


def health(directory):
    record = read(directory / 'server.json')
    if record['state'] != 'running':
        raise ValueError('This delivery server is not running')
    # Never use a foreign URL from a modified receipt.
    if type(record['port']) is not int or not 1 <= record['port'] <= 65535:
        raise ValueError('Invalid local port')
    origin = f"http://127.0.0.1:{record['port']}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(origin + '/api/delivery/health', timeout=3) as response:
        current = json.load(response)
    if current != {k: record[k] for k in ('instance_id', 'pid', 'port')} | {'status': 'PASS'}:
        raise ValueError('Health identity differs from this server receipt')
    return current | {'url': origin + '/business'}


def stop(directory):
    value = health(directory)
    # Request the original controller to stop; never kill a PID read from disk.
    save(directory / 'stop-request.json', {'instance_id': value['instance_id']})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record = read(directory / 'server.json')
        if record['instance_id'] != value['instance_id']:
            raise ValueError('Controller identity changed')
        if record['state'] == 'stopped':
            return dict(status='PASS', stopped=True, instance_id=value['instance_id'])
        time.sleep(.1)
    raise TimeoutError('Server did not acknowledge shutdown')


def serve(directory, port):
    import uvicorn
    from dashboard.api.main import create_app, STATIC_DIR
    if not (STATIC_DIR / 'index.html').is_file():
        raise ValueError('Build dashboard/frontend before starting the delivery server')
    if read(directory / 'delivery.json')['status'] != 'PASS':
        raise ValueError('Run the delivery demo successfully before serving its evidence')
    # Re-verify recovery hashes before exposing a ready server.
    from dashboard.api.recovery import RecoveryEvidence
    RecoveryEvidence(directory / 'recovery-index.json', directory / 'ci').list()
    record_path = directory / 'server.json'
    if record_path.exists() and read(record_path)['state'] == 'running':
        raise ValueError('Existing running receipt; stop that server or choose a new demo directory')
    identity = dict(instance_id=uuid4().hex, pid=os.getpid(), port=port)
    app = create_app(business_output=directory / 'business-api', recovery_report=directory / 'recovery-index.json',
                     recovery_root=directory / 'ci', delivery_identity=identity)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    finished = threading.Event()
    def control():
        while not finished.wait(.1):
            if server.started and not record_path.exists():
                save(record_path, identity | {'state': 'running'})
            if server.started and record_path.exists() and read(record_path).get('instance_id') != identity['instance_id']:
                save(record_path, identity | {'state': 'running'})
            request = directory / 'stop-request.json'
            if request.exists() and read(request).get('instance_id') == identity['instance_id']:
                server.should_exit = True
                return
    thread = threading.Thread(target=control, daemon=True)
    thread.start()
    try:
        print(f'Delivery dashboard: http://127.0.0.1:{port}/business', flush=True)
        server.run()
        if not server.started:
            raise RuntimeError('Delivery server did not start')
    finally:
        finished.set()
        thread.join(timeout=2)
        save(record_path, identity | {'state': 'stopped'})
    return dict(status='PASS', stopped=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['demo', 'serve', 'health', 'stop', 'verify-bundle'])
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--port', type=int, default=8035)
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    try:
        if args.command == 'verify-bundle':
            if not args.archive:
                parser.error('--archive is required')
            result = verify_bundle(args.archive)
        else:
            if not args.directory:
                parser.error('--directory is required (fresh for demo)')
            directory = owned_path(args.directory)
            if args.command == 'demo':
                if os.name != 'nt':
                    raise ValueError('D35 interactive delivery currently supports Windows; Linux uses verify_v2 container CI')
                import ctypes
                if ctypes.windll.shell32.IsUserAnAdmin():
                    sys.path.insert(0, str(ROOT / 'scripts'))
                    from run_unprivileged import run
                    return run([str(ROOT / 'scripts/delivery.py'), 'demo', '--directory', str(directory)],
                               ROOT / 'artifacts/delivery-launchers' / (uuid4().hex + '.json'), 3600)
                from agentcheck_biz.v2.process import own_descendants
                own_descendants()
                result = demo(directory)
            elif args.command == 'serve':
                if not 1 <= args.port <= 65535:
                    parser.error('port must be between 1 and 65535')
                result = serve(directory, args.port)
            else:
                result = {'health': health, 'stop': stop}[args.command](directory)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps(dict(status='ERROR', error=type(exc).__name__ + ': ' + str(exc)), ensure_ascii=False))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())

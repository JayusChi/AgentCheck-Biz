"""D35 archive integrity, isolated page evidence and stop identity boundaries."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient
from agentcheck_biz.delivery import verify_bundle, owned_path, health, stop, recovery_index, run_release_gate
import os
from dashboard.api.main import create_app


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows runners may expose TEMP through an 8.3 short path.
        # Match the resolved checkout root used by the real delivery module.
        self.root = Path(self.temp.name).resolve()

    def archive(self, files=None, extra=None):
        files = files if files is not None else {'hello.py': b'print(1)'}
        path = self.root / 'source.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in (files | (extra or {})).items():
                archive.writestr(name, data)
            archive.writestr('SOURCE-SHA256.json', json.dumps([
                dict(path=n, sha256=hashlib.sha256(d).hexdigest()) for n, d in files.items()]))
        return path

    def test_archive_checks_exact_bytes(self):
        self.assertEqual(verify_bundle(self.archive())['files'], 1)

    def test_extra_member_is_rejected(self):
        with self.assertRaises(ValueError):
            verify_bundle(self.archive(extra={'private.txt': b'secret'}))

    def test_missing_or_corrupt_member_is_rejected(self):
        path = self.root / 'source.zip'
        for actual in ({}, {'hello.py': b'changed'}):
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('SOURCE-SHA256.json', json.dumps([dict(path='hello.py', sha256='0' * 64)]))
                for name, data in actual.items():
                    archive.writestr(name, data)
            with self.assertRaises(ValueError):
                verify_bundle(path)

    def test_empty_bundle_cannot_pass(self):
        with self.assertRaises(ValueError):
            verify_bundle(self.archive(files={}))

    def test_paths_and_runtime_files_refused(self):
        for name in ('../outside.py', 'C:/outside.py', '/outside.py', '.env', 'artifacts/secret.json'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                verify_bundle(self.archive(files={name: b'data'}))

    def test_symlink_member_refused(self):
        path = self.root / 'source.zip'
        data = b'outside'
        info = zipfile.ZipInfo('link.py')
        info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(info, data)
            archive.writestr('SOURCE-SHA256.json', json.dumps([dict(path='link.py', sha256=hashlib.sha256(data).hexdigest())]))
        with self.assertRaises(ValueError):
            verify_bundle(path)

    def test_foreign_output_and_root_refused(self):
        with patch('agentcheck_biz.delivery.ROOT', self.root):
            for path in (self.root, self.root / 'artifacts', self.root / '../foreign'):
                with self.assertRaises(ValueError):
                    owned_path(path)
            self.assertEqual(owned_path(self.root / 'artifacts/new'), self.root / 'artifacts/new')

    def test_dashboard_uses_selected_empty_history_and_missing_evidence(self):
        with TestClient(create_app(business_output=self.root / 'jobs', recovery_report=self.root / 'missing.json',
                                  recovery_root=self.root, delivery_identity={'instance_id': 'fixture', 'pid': 123, 'port': 8035})) as client:
            self.assertEqual(client.get('/api/business/runs').json(), [])
            self.assertEqual(client.get('/api/business/recovery/runs').json(), {'ready': False, 'runs': []})
            self.assertEqual(client.get('/api/delivery/health').json()['instance_id'], 'fixture')
            self.assertEqual(len(client.get('/api/business/cases').json()), 12)

    def test_damaged_recovery_evidence_never_displays_success(self):
        evidence = self.root / 'bad.json'
        evidence.write_text('{"status":"FAIL"}', encoding='utf8')
        with TestClient(create_app(business_output=self.root / 'jobs', recovery_report=evidence, recovery_root=self.root)) as client:
            self.assertEqual(client.get('/api/business/recovery/runs').status_code, 503)

    def test_stop_checks_identity_before_writing_request(self):
        with patch('agentcheck_biz.delivery.health', side_effect=ValueError('wrong instance')):
            with self.assertRaises(ValueError):
                stop(self.root)
        self.assertFalse((self.root / 'stop-request.json').exists())

    def test_stopped_receipt_does_not_contact_port(self):
        (self.root / 'server.json').write_text('{"state":"stopped"}')
        with patch('urllib.request.build_opener', side_effect=AssertionError('contacted')):
            with self.assertRaises(ValueError):
                health(self.root)

    def test_recovery_index_rejects_incomplete_ci(self):
        ci = self.root / 'artifacts/ci'
        (ci / 'public').mkdir(parents=True)
        (ci / 'private/integration/recovery-check').mkdir(parents=True)
        (ci / 'public/summary.json').write_text('{"status":"ERROR","scope":"all"}')
        (ci / 'private/integration/recovery-check/stage.json').write_text('{"status":"PASS","experiments":4}')
        with patch('agentcheck_biz.delivery.ROOT', self.root), self.assertRaises(ValueError):
            recovery_index(ci, ci / 'index.json')
        self.assertFalse((ci / 'index.json').exists())

    def gate(self, status, code, pid=123):
        directory = self.root / 'gate'
        def launch(*args, **kwargs):
            (directory / 'independent-recheck.json').write_text(json.dumps(dict(status=status,
                worker_pid=pid, controller_pid=os.getpid(), planned=1, executed=1)))
            return dict(pid=123, exit_code=code)
        with patch('agentcheck_biz.network_acceptance.process.launch', side_effect=launch):
            return run_release_gate(directory, {'id': 'test'})

    def test_real_gate_fail_and_exit_one_are_preserved(self):
        self.assertEqual(self.gate('FAIL', 1)['status'], 'FAIL')

    def test_gate_nonzero_exit_cannot_be_reported_as_pass(self):
        with self.assertRaises(ValueError):
            self.gate('PASS', 1)

    def test_gate_result_must_belong_to_launched_process(self):
        with self.assertRaises(ValueError):
            self.gate('FAIL', 1, pid=999999)


if __name__ == '__main__':
    unittest.main()

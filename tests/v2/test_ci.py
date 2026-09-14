"""D33 boundary tests: publishable evidence, source export, fixed model-free plan."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from xml.etree import ElementTree as ET

from agentcheck_biz.ci.artifacts import projection, publish, source_bundle, ensure_report
from agentcheck_biz.ci.worker import configuration, block_external
from agentcheck_biz.provenance import REPO_ROOT
from scripts.verify_v2 import clean_environment, stage_status


class CITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = dict(scope='core',status='PASS',steps=[dict(name='core-tests',status='PASS',exit_code=0,tests=10)])

    def sources(self, names=None):
        (self.root/'ci').mkdir(exist_ok=True)
        (self.root/'ci/source-files.json').write_text(json.dumps(names or ['safe.py']),encoding='utf8')
        (self.root/'safe.py').write_text('print("synthetic fixture")\n',encoding='utf8')

    def test_projection_discards_credentials_payloads_and_paths(self):
        value = deepcopy(self.report)
        value.update(token='canary',authorization='canary',error='canary',path='canary',environment={'KEY':'canary'})
        value['steps'][0].update(log='canary',message='canary',exception='canary')
        value['observed'] = dict(ticket_count=2,body='canary',ticket_tool_calls='canary')
        result = projection(value)
        self.assertNotIn('canary',json.dumps(result))
        self.assertEqual(result['observed'],dict(ticket_count=2))

    def test_failure_is_published_in_all_formats(self):
        self.report['status'] = self.report['steps'][0]['status'] = 'FAIL'
        public = self.root/'public'
        publish(self.report,public)
        self.assertEqual(json.loads((public/'summary.json').read_text())['status'],'FAIL')
        self.assertIn('FAIL',(public/'report.md').read_text())
        self.assertEqual(ET.parse(public/'junit.xml').getroot().get('failures'),'1')

    def test_pending_stays_skipped_and_error_stays_error(self):
        self.report.update(status='ERROR',steps=[dict(name='configuration',status='ERROR'),dict(name='integration',status='INCONCLUSIVE')])
        publish(self.report,self.root/'public')
        suite = ET.parse(self.root/'public/junit.xml').getroot()
        self.assertEqual((suite.get('tests'),suite.get('errors'),suite.get('skipped')),('2','1','1'))

    def test_unknown_stage_cannot_be_used_to_smuggle_string(self):
        self.report['steps'][0]['name']='Authorization: canary'
        with self.assertRaises(ValueError):projection(self.report)

    def test_string_cannot_be_used_as_numeric_verdict(self):
        self.report['steps'][0]['tests']='canary'
        with self.assertRaises(ValueError):projection(self.report)

    def test_boolean_not_accepted_as_test_count(self):
        self.report['steps'][0]['tests']=True
        with self.assertRaises(ValueError):projection(self.report)

    def test_unsafe_digest_refused(self):
        self.report['source_sha256']='canary'
        with self.assertRaises(ValueError):projection(self.report)

    def test_preexisting_extra_artifact_refused(self):
        target=self.root/'public';target.mkdir();(target/'secret.log').write_text('canary')
        with self.assertRaises(ValueError):publish(self.report,target)

    def test_setup_failure_also_has_three_publishable_reports(self):
        result=ensure_report(self.root/'public','core')
        self.assertEqual(result['status'],'ERROR')
        self.assertEqual({p.name for p in (self.root/'public').iterdir()},{'summary.json','report.md','junit.xml'})

    def test_fallback_preserves_business_failure(self):
        self.report['status']=self.report['steps'][0]['status']='FAIL'
        publish(self.report,self.root/'public')
        self.assertEqual(ensure_report(self.root/'public','core')['status'],'FAIL')

    def test_source_identity_changes_with_ci_contract_content(self):
        self.sources()
        first=source_bundle(self.root,self.root/'first.zip')
        (self.root/'safe.py').write_text('changed')
        second=source_bundle(self.root,self.root/'second.zip')
        self.assertNotEqual(first['source_sha256'],second['source_sha256'])

    def test_archive_is_exact_allowlist_plus_digest_manifest(self):
        self.sources()
        (self.root/'.env').write_text('TOKEN=canary')
        (self.root/'data.sqlite').write_text('real business canary')
        source_bundle(self.root,self.root/'source.zip')
        with zipfile.ZipFile(self.root/'source.zip') as archive:
            self.assertEqual(set(archive.namelist()),{'safe.py','SOURCE-SHA256.json'})
            self.assertFalse(any(b'canary' in archive.read(n) for n in archive.namelist()))

    def test_env_cannot_be_allowlisted(self):
        self.sources(['.env']);(self.root/'.env').write_text('canary')
        with self.assertRaises(ValueError):source_bundle(self.root,self.root/'source.zip')

    def test_cache_cannot_be_allowlisted(self):
        self.sources(['.cache/data.py'])
        with self.assertRaises(ValueError):source_bundle(self.root,self.root/'source.zip')

    def test_path_escape_refused(self):
        self.sources(['../outside.py'])
        with self.assertRaises(ValueError):source_bundle(self.root,self.root/'source.zip')

    def test_missing_source_fails_instead_of_silent_omission(self):
        self.sources(['missing.py'])
        with self.assertRaises(FileNotFoundError):source_bundle(self.root,self.root/'source.zip')

    def test_duplicate_source_refused(self):
        self.sources(['safe.py','safe.py'])
        with self.assertRaises(ValueError):source_bundle(self.root,self.root/'source.zip')

    def test_credential_shaped_content_refused(self):
        self.sources();(self.root/'safe.py').write_text('ghp_'+'A'*36)
        with self.assertRaises(ValueError):source_bundle(self.root,self.root/'source.zip')

    def test_linked_file_refused_before_reading(self):
        self.sources()
        original=Path.lstat
        def injected(path, *args, **kwargs):
            if path.name=='safe.py':
                from types import SimpleNamespace
                return SimpleNamespace(st_mode=0o120777,st_file_attributes=0)
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',injected):
            with self.assertRaises(ValueError):source_bundle(self.root,self.root/'source.zip')

    def test_environment_drops_provider_and_github_credentials(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'canary','ANTHROPIC_API_KEY':'canary','GITHUB_TOKEN':'canary','DATABASE_URL':'canary'}):
            env=clean_environment()
        self.assertNotIn('canary',json.dumps(env))
        self.assertEqual(env['PYTHON_DOTENV_DISABLED'],'1')

    def test_loopback_guard_refuses_remote_model_endpoint(self):
        with self.assertRaises(PermissionError):block_external('socket.connect',(None,('api.openai.com',443)))
        block_external('socket.connect',(None,('127.0.0.1',43210)))

    def test_missing_stage_result_cannot_pass_with_zero_exit(self):
        self.assertEqual(stage_status(0,{}),'ERROR')

    def test_exit_and_verdict_must_agree(self):
        for code,status in [(0,'PASS'),(1,'FAIL'),(2,'INCONCLUSIVE'),(3,'ERROR')]:
            self.assertEqual(stage_status(code,dict(status=status)),status)
            self.assertEqual(stage_status(124,dict(status=status)),'ERROR')
        self.assertEqual(stage_status(0,dict(status='FAIL')),'ERROR')

    def test_frozen_configuration_validates_without_allocating_services(self):
        with patch('agentcheck_biz.persistence.runtime.PostgresRuntime.start',side_effect=AssertionError('allocated')):
            self.assertEqual(configuration()['model_requests'],0)

    def test_plan_cannot_enable_live_or_remove_recovery_cases(self):
        data=configuration()
        for changed in [dict(data,live=True),dict(data,model_requests=1),dict(data,recovery_scenarios=[])]:
            with patch('agentcheck_biz.ci.worker.json.loads',return_value=changed):
                with self.assertRaises(ValueError):configuration()

    def test_workflow_uploads_only_three_explicit_files_per_job(self):
        import yaml
        workflow=yaml.safe_load((REPO_ROOT/'.github/workflows/verify-v2.yml').read_text())
        self.assertEqual(workflow['permissions'],{'contents':'read'})
        for job in workflow['jobs'].values():
            action=job['steps'][-1]
            paths=action['with']['path'].splitlines()
            self.assertEqual(len(paths),3)
            self.assertTrue(all('/public/' in p and '*' not in p for p in paths))
            self.assertEqual(action['if'],'always()')

    def native_archive(self, link):
        import io, tarfile
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w') as archive:
            file=tarfile.TarInfo('lib/actual.so');file.size=4
            archive.addfile(file,io.BytesIO(b'test'))
            member=tarfile.TarInfo('lib/alias.so');member.type=tarfile.SYMTYPE;member.linkname=link
            archive.addfile(member)
        data.seek(0)
        return tarfile.open(fileobj=data,mode='r:')

    def test_linux_library_link_materialized_without_symlink(self):
        from agentcheck_biz.persistence.runtime import extract_native
        with self.native_archive('actual.so') as archive:extract_native(archive,self.root/'native')
        path=self.root/'native/lib/alias.so'
        self.assertEqual(path.read_bytes(),b'test')
        self.assertFalse(path.is_symlink())

    def test_archive_escape_or_link_cycle_refused_before_writes(self):
        from agentcheck_biz.persistence.runtime import extract_native
        for target in ['../../secret','/etc/passwd','alias.so']:
            with self.native_archive(target) as archive:
                with self.assertRaises(ValueError):extract_native(archive,self.root/'native')
            self.assertFalse((self.root/'native').exists())


if __name__=='__main__':unittest.main()

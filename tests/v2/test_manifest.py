"""D31 fixed plans, actual slot execution, independent checks and isolation."""
from copy import deepcopy
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from agentcheck_biz.checks import load_json
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.persistence.runtime import child_environment
from agentcheck_biz.network_acceptance.process import launch
from agentcheck_biz.network_acceptance.verify import hashes
from agentcheck_biz.reports import save_json
from agentcheck_biz.v2.manifest import build,catalog,validate,read,source_digest,capabilities
from agentcheck_biz.v2.runner import allocate,save
from agentcheck_biz.v2.evidence import aggregate,check,within
from dashboard.api.capabilities import create_capabilities_router


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=REPO_ROOT/'tmp')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.m=build(['network:ticket_P00_transparent','continuity:saved-tool-call'])

    def reject(self,mutate):
        m=deepcopy(self.m)
        mutate(m)
        with patch('agentcheck_biz.persistence.runtime.PostgresRuntime.start',side_effect=AssertionError('allocated')),\
             patch('examples.gitea_target.runtime.GiteaRuntime.start',side_effect=AssertionError('allocated')):
            with self.assertRaises(ValueError):allocate(m,self.root/'rejected')
        self.assertFalse((self.root/'rejected').exists())

    def test_all_registered_profiles_have_valid_frozen_shapes(self):
        validate(build(list(catalog())),environment=False)
        self.assertEqual(len(catalog()),34)

    def test_zero_cases_rejected(self):self.reject(lambda m:m.update(slots=[]))
    def test_duplicate_slot_rejected(self):self.reject(lambda m:m['slots'][1].update(slot_id='s001'))
    def test_foreign_source_rejected(self):self.reject(lambda m:m.update(source_sha256='0'*64))
    def test_old_schema_rejected(self):self.reject(lambda m:m.update(schema_version='agentcheck-manifest/1'))
    def test_unknown_evidence_version_rejected(self):self.reject(lambda m:m.update(evidence_version='agentcheck-evidence/99'))
    def test_unknown_adapter_rejected(self):self.reject(lambda m:m['slots'][0]['adapter'].update(version='arbitrary.module'))
    def test_unknown_object_version_rejected(self):self.reject(lambda m:m['slots'][0]['object'].update(version='future'))
    def test_invalid_last_case_before_first_allocation(self):self.reject(lambda m:m['slots'][-1]['case']['request'].update(customer_id=''))
    def test_initial_state_change_rejected(self):self.reject(lambda m:m['slots'][0].update(initial_state=[]))
    def test_fault_rule_change_rejected(self):self.reject(lambda m:m['slots'][0]['fault'].update(rule={'code':'print(1)'}))
    def test_sql_field_rejected(self):self.reject(lambda m:m['slots'][0].update(sql='DELETE FROM tickets'))
    def test_budget_change_rejected(self):self.reject(lambda m:m['slots'][0]['budget'].update(tool_limit=999))
    def test_recovery_change_rejected(self):self.reject(lambda m:m['slots'][0]['recovery'].update(strategy='always_retry'))
    def test_expected_effect_change_rejected(self):self.reject(lambda m:m['slots'][0]['expected'].update(resource_count=2))
    def test_invalid_parallel_rejected(self):self.reject(lambda m:m.update(execution={'parallel':3}))
    def test_float_parallel_rejected(self):self.reject(lambda m:m.update(execution={'parallel':1.0}))
    def test_boolean_expected_count_rejected(self):self.reject(lambda m:m['slots'][0]['expected'].update(resource_count=True))
    def test_boolean_case_limit_rejected(self):self.reject(lambda m:m['slots'][0]['case']['limits'].update(max_client_attempts=True))
    def test_slot_path_traversal_rejected(self):self.reject(lambda m:m['slots'][0].update(slot_id='../foreign'))

    def test_missing_environment_before_allocation(self):
        caps=capabilities(resources=True)
        caps['resources']['postgres']=False
        with patch('agentcheck_biz.v2.manifest.capabilities',return_value=caps):
            with self.assertRaisesRegex(ValueError,'PostgreSQL'):allocate(self.m,self.root/'no-runtime')
        self.assertFalse((self.root/'no-runtime').exists())

    def test_missing_dependency_before_allocation(self):
        caps=capabilities(resources=True)
        caps['resources']['packages']['langgraph']='unknown'
        with patch('agentcheck_biz.v2.manifest.capabilities',return_value=caps):
            with self.assertRaisesRegex(ValueError,'dependency'):allocate(self.m,self.root/'no-dependency')
        self.assertFalse((self.root/'no-dependency').exists())

    def test_duplicate_json_keys_rejected(self):
        path=self.root/'bad.json'
        path.write_text('{"slots": [], "slots": [1]}')
        with self.assertRaisesRegex(ValueError,'Duplicate JSON'):read(path)

    def test_nonfinite_json_rejected(self):
        path=self.root/'bad.json'
        path.write_text('{"budget": NaN}')
        with self.assertRaisesRegex(ValueError,'Non-finite'):read(path)

    def test_all_slots_exist_before_execution(self):
        directory,report=allocate(self.m,self.root)
        self.assertEqual([s['state'] for s in report['slots']],['not_started']*2)
        for spec in self.m['slots']:
            self.assertTrue((directory/'slots'/spec['slot_id']/'slot.json').is_file())
            self.assertEqual(list((directory/'slots'/spec['slot_id']/'payload').iterdir()),[])
        self.assertEqual(check(directory,self.m)['status'],'INCONCLUSIVE')

    def test_empty_aggregate_never_passes(self):
        self.assertEqual(aggregate([]),{})
        directory,_=allocate(self.m,self.root)
        report=load_json(directory/'summary.json')
        report['slots']=[]
        save(directory/'summary.json',report)
        with self.assertRaisesRegex(ValueError,'Missing'):check(directory,self.m)

    def test_discovery_reports_runtime_version_and_requested_address(self):
        app=FastAPI()
        app.include_router(create_capabilities_router())
        with TestClient(app,base_url='http://127.0.0.1:8197') as client:
            data=client.get('/api/business/capabilities').json()
        self.assertEqual(data['backend_version'],'2.31.0')
        self.assertEqual(data['backend_address'],'http://127.0.0.1:8197')
        self.assertEqual(data['source_sha256'],source_digest())
        self.assertEqual(data['execution']['parallel_max'],2)
        self.assertEqual(data['features']['recovery_evidence'],1)

    def test_source_digest_covers_sql_schema_and_case(self):
        from agentcheck_biz.v2.manifest import source_files
        files=source_files()
        for name in ['agentcheck_biz/continuity/001_budget.sql','schema/v2-manifest.schema.json','cases/tickets/full/T01.json']:
            self.assertIn(name,files)

    def test_evidence_path_cannot_escape_slot(self):
        with self.assertRaises(ValueError):within(self.root,'../foreign')


class ManifestExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(os.environ.get('AGENTCHECK_D31_EVIDENCE',str(REPO_ROOT/'artifacts/v2'/('d31-tests-'+uuid4().hex))))
        cls.root.mkdir(parents=True,exist_ok=True)
        cls.runs={}
        selections={'serial':list(catalog()),'parallel':[
            'network:ticket_P03_drop','network:ticket_P04_miss',
            'continuity:concurrent-resume','continuity:concurrent-resume',
            'continuity:insufficient-read-evidence','continuity:gitea-resume']}
        for label,profiles in selections.items():
            manifest=build(profiles,parallel=1 if label=='serial' else 2)
            path=cls.root/(label+'-manifest.json')
            save_json(path,manifest)
            receipt=launch(['-m','agentcheck_biz.v2','batch','--manifest',str(path),'--output',str(cls.root/label)],cls.root/(label+'.log'),timeout=5000)
            save_json(cls.root/(label+'-process.json'),receipt)
            if receipt['exit_code']!=0:raise AssertionError('D31 '+label+' failed; inspect '+receipt['log'])
            result=json.loads(Path(receipt['log']).read_text(encoding='utf8').splitlines()[-1])
            directory=Path(result['directory'])
            cls.runs[label]=(manifest,path,directory,load_json(directory/'summary.json'))
            checked=cls.offline(label,path,directory)
            if checked['status']!='PASS':raise AssertionError(checked)

    @classmethod
    def offline(cls,label,path,directory):
        before=hashes(directory)
        receipt=launch(['-m','agentcheck_biz.v2','check','--manifest',str(path),'--output',str(directory)],cls.root/(label+'-check.log'),timeout=180)
        result=json.loads(Path(receipt['log']).read_text(encoding='utf8').splitlines()[-1])
        if before!=hashes(directory):raise AssertionError('Offline check modified evidence')
        if result.get('verifier_pid')==os.getpid():raise AssertionError('Same-process oracle')
        save_json(cls.root/(label+'-check.json'),dict(result=result,process=receipt))
        return result

    def test_serial_full_catalog(self):
        report=self.runs['serial'][3]
        self.assertEqual(report['status'],'PASS')
        self.assertEqual(len(report['slots']),34)
        times=[s['process'] for s in report['slots']]
        self.assertTrue(all(a['finished_at']<=b['started_at'] for a,b in zip(times,times[1:])))

    def test_parallel_actually_overlaps(self):
        slots=self.runs['parallel'][3]['slots']
        for a,b in zip(slots[::2],slots[1::2]):
            self.assertLess(max(a['process']['started_at'],b['process']['started_at']),min(a['process']['finished_at'],b['process']['finished_at']))

    def test_fault_misses_remain_in_denominator(self):
        agg=self.runs['serial'][3]['aggregate']
        self.assertEqual(agg['coverage:not_covered']['count'],2)
        self.assertEqual(agg['planned']['count'],34)

    def test_failed_observation_remains_despite_later_recovery(self):
        report=self.runs['serial'][3]
        slot=next(s for s in report['slots'] if s['profile']=='continuity:insufficient-read-evidence')
        observed=slot['inspection']['observed']
        self.assertEqual((observed['business_status'],observed['observation_status'],observed['final_business_status']),('INCONCLUSIVE','failed','PASS'))
        self.assertIn(slot['slot_id'],report['aggregate']['observation_status:failed']['slot_ids'])

    def test_expected_bug_is_still_business_fail(self):
        report=self.runs['serial'][3]
        slot=next(s for s in report['slots'] if s['profile']=='network:B_unsafe_loss_retry')
        self.assertTrue(slot['matched'])
        self.assertEqual(slot['inspection']['observed']['business_status'],'FAIL')
        self.assertIn(slot['slot_id'],report['aggregate']['business_status:FAIL']['slot_ids'])

    def test_all_numbers_link_to_slots(self):
        for *_,report in self.runs.values():
            ids={s['slot_id'] for s in report['slots']}
            for group in report['aggregate'].values():
                self.assertEqual(group['count'],len(group['slot_ids']))
                self.assertTrue(set(group['slot_ids'])<=ids)

    def test_parallel_storage_leases_and_logs_independently_bound(self):
        slots=self.runs['parallel'][3]['slots'][2:4]
        for key in ('run_id','job_id','thread_id','experiment_id','environment_id','postgres_system','gitea_instance'):
            self.assertNotEqual(slots[0]['inspection']['identity'][key],slots[1]['inspection']['identity'][key])
        for slot in slots:
            run=Path(slot['inspection']['raw_path'])
            db=load_json(run/'completed.json')['postgres']
            self.assertEqual({r['job_id'] for r in db['ac_lease_events']},{slot['inspection']['identity']['job_id']})
            self.assertEqual(slot['inspection']['observed']['details']['worker_processes'],5)

    def copied(self,label):
        m,_,directory,_=self.runs['parallel']
        target=self.root/('negative-'+label)
        shutil.copytree(directory,target)
        return m,target

    def test_deleted_slot_refused(self):
        m,directory=self.copied('missing-slot')
        report=load_json(directory/'summary.json');report['slots'].pop()
        save(directory/'summary.json',report)
        with self.assertRaisesRegex(ValueError,'Missing'):check(directory,m)

    def test_changed_raw_log_refused(self):
        m,directory=self.copied('changed-log')
        file=next((directory/'slots/s001/payload').rglob('proxy-events.jsonl'))
        file.write_text(file.read_text(encoding='utf8')+'{}\n',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'hash changed'):check(directory,m)

    def test_forged_aggregate_refused(self):
        m,directory=self.copied('aggregate')
        report=load_json(directory/'summary.json');report['aggregate']['planned']['count']=1
        save(directory/'summary.json',report)
        with self.assertRaisesRegex(ValueError,'Aggregate'):check(directory,m)

    def test_foreign_slot_outcome_even_if_rehashed_refused(self):
        m,directory=self.copied('foreign')
        source=directory/'slots/s001/payload/outcome.json'
        target=directory/'slots/s002/payload/outcome.json'
        shutil.copyfile(source,target)
        report=load_json(directory/'summary.json')
        report['slots'][1]['evidence_sha256']=hashes(target.parent)
        save(directory/'slots/s002/slot.json',report['slots'][1]);save(directory/'summary.json',report)
        with self.assertRaisesRegex(ValueError,'Foreign slot'):check(directory,m)

    def test_cli_run_refuses_multi_slot_manifest_without_allocation(self):
        receipt=launch(['-m','agentcheck_biz.v2','run','--manifest',str(self.runs['parallel'][1]),'--output',str(self.root/'invalid-run')],self.root/'invalid-run.log')
        self.assertEqual(receipt['exit_code'],3)
        self.assertFalse((self.root/'invalid-run').exists())

    def test_cli_single_run_and_validate(self):
        manifest=build(['network:ticket_P00_transparent'])
        path=self.root/'single-manifest.json';save_json(path,manifest)
        valid=launch(['-m','agentcheck_biz.v2','validate','--manifest',str(path)],self.root/'validate.log')
        self.assertEqual(valid['exit_code'],0)
        step=launch(['-m','agentcheck_biz.v2','run','--manifest',str(path),'--output',str(self.root/'single')],self.root/'single.log')
        self.assertEqual(step['exit_code'],0)
        result=json.loads(Path(step['log']).read_text(encoding='utf8').splitlines()[-1])
        self.assertEqual(self.offline('single',path,Path(result['directory']))['status'],'PASS')

    def test_killed_coordinator_retains_all_slots_and_closes_descendants(self):
        m=build(['continuity:saved-tool-call']*3)
        path=self.root/'interrupted-manifest.json';save_json(path,m)
        output=self.root/'interrupted'
        env=child_environment(dict(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',__PYVENV_LAUNCHER__=sys.executable))
        with (self.root/'interrupted.log').open('wb') as log:
            process=subprocess.Popen([sys._base_executable,'-m','agentcheck_biz.v2','batch','--manifest',str(path),'--output',str(output)],cwd=REPO_ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            handles=[]
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            try:
                end=time.monotonic()+120
                ready=[]
                while time.monotonic()<end and process.poll() is None:
                    ready=list(output.glob('batch-*/slots/s001/payload/d30-continuity-*/postgres/pg-*/ready-1.json'))
                    if ready:break
                    time.sleep(.1)
                self.assertTrue(ready,'Original PG never started')
                directory=next(output.glob('batch-*'))
                worker=load_json(directory/'slots/s001/payload/worker-identity.json')['pid']
                pids=[worker,load_json(ready[0])['pid']]
                for pid in pids:
                    handle=kernel.OpenProcess(0x100000,False,pid)
                    self.assertTrue(handle);handles.append(handle)
                process.kill();process.wait(timeout=10)
                for handle in handles:self.assertEqual(kernel.WaitForSingleObject(handle,10000),0)
                report=load_json(directory/'summary.json')
                self.assertEqual([s['state'] for s in report['slots']],['started','not_started','not_started'])
                result=self.offline('interrupted',path,directory)
                self.assertEqual((result['status'],result['planned'],result['executed']),('INCONCLUSIVE',3,0))
                save_json(self.root/'interrupted-cleanup.json',dict(coordinator_pid=process.pid,exit_code=process.returncode,
                    descendant_pids=pids,owned_handles_signaled=True,all_slots_retained=True))
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=10)
                for handle in handles:kernel.CloseHandle(handle)


if __name__=='__main__':unittest.main()

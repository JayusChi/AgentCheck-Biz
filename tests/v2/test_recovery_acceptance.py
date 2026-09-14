"""D30 offline Agent protocol, real PG budgets/control and process acceptance."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import uuid4
import psycopg
from langchain_core.messages import AIMessage,ToolMessage,messages_to_dict
from agentcheck_biz.checks import load_json
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from agentcheck_biz.persistence.store import new_manifest,BindingError,IDENTITY_KEYS
from agentcheck_biz.persistence.runtime import PostgresRuntime
from agentcheck_biz.persistence.graph import config
from agentcheck_biz.scheduling.store import LeaseLost
from agentcheck_biz.scheduling.demo import wait_expiry
from agentcheck_biz.continuity.store import Budgets,Stopped
from agentcheck_biz.continuity.graph import OfflineModel,tool_call,validate,graph
from agentcheck_biz.continuity.demo import suite,scenarios
from agentcheck_biz.continuity.verify import recheck
from agentcheck_biz.continuity.view import timestamp


def manifest(directory,**limits):
    case=load_json(REPO_ROOT/'cases/tickets/full/T01.json')
    m=new_manifest(directory,case)
    m['side_effect']=dict(kind='ticket',scope='tenant-A',operation_id=m['operation_id'],request=case['request'])
    m['recovery']=dict(version='continuity/1',client='offline-fixed/1',model_limit=2,tool_limit=3,wall_seconds=60)|limits
    return m


class ProtocolTests(unittest.TestCase):
    def setUp(self):self.m=manifest(REPO_ROOT/'tmp/protocol-only')

    def state(self,messages,result=None):
        return {k:self.m[k] for k in IDENTITY_KEYS}|dict(recovery_version='continuity/1',model_requests=0,
            messages=messages_to_dict(messages),phase={0:'new',1:'tool_pending',2:'tool_verified',3:'completed'}[len(messages)],result=result)

    def test_offline_client_returns_real_ai_tool_call(self):
        answer=OfflineModel().invoke([],self.m)
        self.assertIsInstance(answer,AIMessage)
        self.assertEqual(answer.tool_calls,[tool_call(self.m)])
        validate(self.state([answer]),self.m)

    def test_final_client_requires_correlated_tool_message(self):
        with self.assertRaises(BindingError):OfflineModel().invoke([AIMessage(content='success')],self.m)
        with self.assertRaises(BindingError):OfflineModel().invoke([ToolMessage(content='{}',tool_call_id='wrong')],self.m)

    def test_changed_tool_arguments_id_or_name_fail_closed(self):
        for key,value in (('name','arbitrary_tool'),('id','foreign'),('args',dict(operation_id='foreign'))):
            with self.subTest(key=key),self.assertRaises(BindingError):
                validate(self.state([AIMessage(content='',tool_calls=[tool_call(self.m)|{key:value}])]),self.m)

    def test_tool_result_must_equal_saved_business_result(self):
        first=OfflineModel().invoke([],self.m)
        with self.assertRaises(BindingError):
            validate(self.state([first,ToolMessage(content='{"id":1}',tool_call_id=tool_call(self.m)['id'])],dict(id=2)),self.m)

    def test_paid_model_activity_rejected_in_checkpoint(self):
        with self.assertRaises(BindingError):validate(self.state([])|dict(model_requests=1),self.m)

    def test_pg_timestamp_accepts_trimmed_microseconds_and_timezone(self):
        self.assertEqual(timestamp('2026-09-14T10:39:57.85273+08:00'),timestamp('2026-09-14T02:39:57.852730+00:00'))


class BudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.pg=PostgresRuntime(Path(cls.temp.name)/'postgres').start()
        cls.addClassCleanup(cls.pg.close)
        cls.store=Budgets(cls.pg.dsn)
        cls.store.setup()

    def setUp(self):
        self.m=manifest(Path(self.temp.name)/uuid4().hex)
        self.store.create(self.m)
        self.lease=self.store.claim(self.m,str(uuid4()),30)
        self.store.prepare(self.m,self.lease)

    def test_budget_is_atomic_with_job_and_survives_new_connection(self):
        b=Budgets(self.pg.dsn).budget(self.m)
        self.assertEqual((b['model_calls'],b['tool_calls']),(0,0))
        self.assertAlmostEqual((timestamp(b['deadline'])-timestamp(b['created_at'])).total_seconds(),60,places=2)

    def test_no_refund_after_failed_dispatch_or_restart(self):
        receipt=self.store.charge(self.m,self.lease,'tool',dict(operation='failed'))
        b=Budgets(self.pg.dsn).budget(self.m)
        self.assertEqual(b['tool_calls'],1)
        self.assertEqual(b['events'][0]['call_id'],receipt['call_id'])

    def test_duplicate_reservation_cannot_authorize_a_second_dispatch(self):
        call=str(uuid4())
        self.store.charge(self.m,self.lease,'tool',{},call)
        with self.assertRaises(Stopped):self.store.charge(self.m,self.lease,'tool',{},call)
        self.assertEqual(self.store.budget(self.m)['tool_calls'],1)

    def test_concurrent_charges_cannot_exceed_limit(self):
        def charge(_):
            try:self.store.charge(self.m,self.lease,'tool',{});return True
            except Stopped:return False
        with ThreadPoolExecutor(max_workers=6) as pool:result=list(pool.map(charge,range(6)))
        self.assertEqual(sum(result),3)
        self.assertEqual(self.store.budget(self.m)['tool_calls'],3)
        self.assertEqual(self.store.budget(self.m)['stop_reason'],'tool_budget_exhausted')

    def test_model_exhaustion_latches_across_new_store_and_resume(self):
        for _ in range(2):self.store.charge(self.m,self.lease,'model',{})
        with self.assertRaises(Stopped):self.store.charge(self.m,self.lease,'model',{})
        with self.assertRaises(Stopped):Budgets(self.pg.dsn).charge(self.m,self.lease,'tool',{})

    def test_deadline_cannot_be_extended_in_sql(self):
        with self.assertRaises(psycopg.errors.CheckViolation),self.store.connect() as conn:
            conn.execute("UPDATE ac_recovery_budgets SET deadline=deadline+interval '1 hour' WHERE job_id=%s",(self.m['job_id'],))

    def test_consumed_budget_cannot_reset_in_sql(self):
        self.store.charge(self.m,self.lease,'tool',{})
        with self.assertRaises(psycopg.errors.CheckViolation),self.store.connect() as conn:
            conn.execute('UPDATE ac_recovery_budgets SET tool_calls=0 WHERE job_id=%s',(self.m['job_id'],))

    def test_unleased_direct_charge_rejected(self):
        with self.assertRaises(psycopg.errors.CheckViolation),self.store.connect() as conn:
            conn.execute('UPDATE ac_recovery_budgets SET tool_calls=1 WHERE job_id=%s',(self.m['job_id'],))

    def test_changed_manifest_limits_rejected(self):
        foreign=deepcopy(self.m)
        foreign['recovery']['tool_limit']=999
        with self.assertRaises(BindingError):self.store.budget(foreign)

    def test_invalid_boolean_and_negative_limits_rejected(self):
        for v in (True,-1,1.5):
            with self.assertRaises(BindingError):self.store.create(manifest(Path(self.temp.name)/uuid4().hex,model_limit=v))

    def test_abort_is_idempotent_and_blocks_resume(self):
        self.assertTrue(self.store.abort(self.m))
        self.assertFalse(self.store.abort(self.m))
        with self.assertRaises(Stopped):Budgets(self.pg.dsn).claim(self.m,str(uuid4()))
        self.assertEqual(len(self.store.budget(self.m)['events']),1)

    def test_abort_fences_old_model_tool_and_business_results(self):
        self.store.transition(self.m,self.lease,'sent_unknown',dict(reason='send'))
        self.store.abort(self.m)
        for kind in ('model','tool'):
            with self.assertRaises(LeaseLost):self.store.charge(self.m,self.lease,kind,{})
        with self.assertRaises(LeaseLost):self.store.transition(self.m,self.lease,'confirmed',dict(complete=True),dict(id=1))

    def test_abort_fences_an_already_open_official_saver(self):
        with self.store.saver(self.m,self.lease) as saver:
            self.store.abort(self.m)
            with self.assertRaises(psycopg.errors.CheckViolation):graph(saver,self.m).update_state(config(self.m),dict(phase='completed'))

    def test_expired_worker_cannot_spend_new_owner_budget(self):
        self.store.charge(self.m,self.lease,'model',{})
        before=self.store.budget(self.m)
        self.store.heartbeat(self.m,self.lease,1)
        wait_expiry(self.store,self.m)
        current=self.store.claim(self.m,str(uuid4()),30)
        with self.assertRaises(LeaseLost):self.store.charge(self.m,self.lease,'tool',{})
        self.store.charge(self.m,current,'tool',{})
        after=self.store.budget(self.m)
        self.assertEqual(after['deadline'],before['deadline'])
        self.assertEqual((after['model_calls'],after['tool_calls']),(1,1))

    def test_absolute_deadline_blocks_new_dispatch_and_claim(self):
        m=manifest(Path(self.temp.name)/uuid4().hex,wall_seconds=1)
        self.store.create(m)
        lease=self.store.claim(m,str(uuid4()),1)
        wait_expiry(self.store,m)
        with self.assertRaises(Stopped):self.store.claim(m,str(uuid4()))

    def test_finished_cannot_bypass_unconfirmed_business(self):
        with self.assertRaises(BindingError):self.store.finish(self.m,self.lease,'finished','fake','fake')


class RecoveryProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.summary=suite(os.environ.get('AGENTCHECK_D30_EVIDENCE',cls.temp.name))
        if cls.summary['status']!='PASS':raise AssertionError(cls.summary)
        cls.directory=Path(cls.summary['summary_path']).parent
        cls.records={r['name']:r for r in cls.summary['scenarios']}

    def test_independent_oracle_accepts_complete_raw_evidence(self):
        value=recheck(self.directory)
        self.assertEqual(value['status'],'PASS',value)

    def test_all_required_faults_have_real_process_results(self):
        self.assertEqual(len(self.records),14)
        self.assertEqual({r['fault'] for r in self.records.values()},{'none','concurrent','storage','deadline','abort','stale','unavailable'})

    def test_saved_model_call_is_not_repeated(self):
        self.assertEqual(self.records['saved-tool-call']['view']['budget']['model_calls'],2)

    def test_tool_budget_remains_exhausted_after_resume(self):
        self.assertEqual(self.records['tool-budget-exhausted']['view']['budget']['tool_calls'],2)

    def test_model_reservation_before_crash_is_not_refunded(self):
        self.assertEqual(self.records['model-reservation-lost']['view']['budget']['model_calls'],1)

    def test_user_abort_and_old_return_never_show_completed(self):
        for name in ('user-abort','late-result-after-abort'):
            self.assertEqual(self.records[name]['view']['execution_state'],'aborted')
            self.assertEqual(self.records[name]['view']['business_verdict'],'INCONCLUSIVE')

    def test_verified_business_can_outlive_agent_budget_stop(self):
        view=self.records['final-model-budget']['view']
        self.assertEqual((view['execution_state'],view['business_verdict']),('budget_stopped','PASS'))

    def test_unavailable_evidence_has_pending_operation_before_restoration(self):
        r=self.records['insufficient-read-evidence']
        self.assertTrue(r['demonstration']['resumed'])
        self.assertTrue(r['demonstration']['pending_operations'])
        self.assertEqual(r['view']['business_verdict'],'PASS')

    def test_non_atomic_empty_observation_never_recreates_issue(self):
        r=self.records['gitea-unknown-empty']
        self.assertEqual(r['final_count'],0)
        self.assertEqual(r['view']['business_verdict'],'INCONCLUSIVE')

    def negative(self,name,file,change):
        with tempfile.TemporaryDirectory() as tmp:
            copied=Path(shutil.copytree(self.directory,Path(tmp)/'copy'))
            path=copied/Path(self.records[name]['run_dir']).relative_to(self.directory)/file
            value=load_json(path)
            change(value)
            save_json(path,value)
            checked=recheck(copied)
            self.assertEqual(checked['status'],'ERROR',checked)

    def test_forged_budget_reset_rejected(self):
        self.negative('saved-tool-call','completed.json',lambda x:x['budget'].update(model_calls=0))

    def test_forged_absolute_deadline_rejected(self):
        self.negative('absolute-deadline','completed.json',lambda x:x['budget'].update(deadline='2099-01-01T00:00:00+00:00'))

    def test_forged_ui_success_rejected(self):
        self.negative('gitea-unknown-empty','completed.json',lambda x:x['view'].update(business_verdict='PASS',pending_operations=[]))

    def test_edited_checkpoint_tool_call_rejected(self):
        self.negative('saved-tool-call','at-gate.json',lambda x:x['checkpoint']['values'].update(messages=[]))

    def test_forged_kill_receipt_rejected(self):
        self.negative('committed-tool-result','crash/termination.json',lambda x:x.update(exit_code=0))

    def test_forged_final_business_json_rejected(self):
        self.negative('committed-tool-result','final.json',lambda x:x['tickets'].clear())

    def api(self,directory=None):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from dashboard.api.recovery import create_recovery_router
        from agentcheck_biz.network_acceptance.verify import hashes
        directory=directory or self.directory
        report=Path(self.temp.name)/('api-'+uuid4().hex+'.json')
        save_json(report,dict(status='PASS',continuity_recheck=dict(status='PASS'),continuity_suite=self.summary,
                             continuity_evidence_sha256=hashes(directory)))
        app=FastAPI()
        app.include_router(create_recovery_router(report,self.directory.parent))
        return TestClient(app)

    def test_api_exposes_four_dimensions_from_actual_process_evidence(self):
        with self.api() as client:
            response=client.get('/api/business/recovery/runs')
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(len(response.json()['runs']),14)
            for name in ('saved-tool-call','insufficient-read-evidence','final-model-budget'):
                r=self.records[name]
                data=client.get('/api/business/recovery/runs/'+r['view']['job_id']).json()
                self.assertEqual({k:data['view'][k] for k in ('execution_state','business_verdict','resumed','pending_operations')},
                                 {k:r['demonstration'][k] for k in ('execution_state','business_verdict','resumed','pending_operations')})
                self.assertTrue(data['read_only'])

    def test_api_moment_switch_preserves_initial_unknown_and_final_confirmation(self):
        r=self.records['insufficient-read-evidence']
        with self.api() as client:
            url='/api/business/recovery/runs/'+r['view']['job_id']
            self.assertEqual(client.get(url).json()['view']['business_verdict'],'INCONCLUSIVE')
            self.assertEqual(client.get(url+'?snapshot=completed').json()['view']['business_verdict'],'PASS')
            self.assertEqual(client.get(url+'?snapshot=../../.env').status_code,422)
            self.assertEqual(client.post(url).status_code,405)
            self.assertEqual(client.get('/api/business/recovery/runs/foreign').status_code,404)

    def test_api_refuses_missing_acceptance_and_tampered_hashed_evidence(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from dashboard.api.recovery import create_recovery_router
        from agentcheck_biz.network_acceptance.verify import hashes
        report=Path(self.temp.name)/'invalid-report.json'
        app=FastAPI()
        app.include_router(create_recovery_router(report,self.directory.parent))
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/business/recovery/runs').json(),dict(ready=False,runs=[]))
            save_json(report,dict(status='ERROR'))
            self.assertEqual(client.get('/api/business/recovery/runs').status_code,503)
            manifest_hashes=hashes(self.directory)
            manifest_hashes['summary.json']='0'*64
            save_json(report,dict(status='PASS',continuity_recheck=dict(status='PASS'),continuity_suite=self.summary,
                                 continuity_evidence_sha256=manifest_hashes))
            self.assertEqual(client.get('/api/business/recovery/runs').status_code,503)


if __name__=='__main__':unittest.main()

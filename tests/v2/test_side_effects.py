"""D29 policy boundaries, fenced PG ledger and real two-object crash recovery."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import uuid4
import psycopg
from agentcheck_biz.checks import load_json
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from agentcheck_biz.persistence.runtime import PostgresRuntime
from agentcheck_biz.persistence.store import new_manifest, BindingError
from agentcheck_biz.scheduling.store import LeaseLost
from agentcheck_biz.scheduling.demo import wait_expiry
from agentcheck_biz.side_effects.store import Operations
from agentcheck_biz.side_effects.policy import Recovery, NeedsVerification, classify
from agentcheck_biz.side_effects.demo import scenarios, suite
from agentcheck_biz.side_effects.verify import recheck


class PolicyTests(unittest.TestCase):
    def test_empty_observation_is_absence_not_confirmation(self):
        self.assertEqual(classify(dict(complete=True,items=[]),dict(title='a')),('absent',None))

    def test_incomplete_observation_never_confirms_even_with_a_matching_row(self):
        for value in (False,1,'true',None):
            self.assertEqual(classify(dict(complete=value,items=[dict(title='a')]),dict(title='a')),('unknown',None))

    def test_duplicate_rows_are_a_conflict(self):
        self.assertEqual(classify(dict(complete=True,items=[dict(title='a')]*2),dict(title='a')),('conflict',None))

    def test_wrong_content_or_state_is_a_conflict(self):
        for item in (dict(title='wrong',state='open'),dict(title='a',state='closed')):
            self.assertEqual(classify(dict(complete=True,items=[item]),dict(title='a',state='open')),('conflict',None))

    def test_complete_unique_matching_result_confirms(self):
        row=dict(id=1,title='a',state='open')
        self.assertEqual(classify(dict(complete=True,items=[row]),dict(title='a',state='open')),('confirmed',row))

    def test_malformed_observation_is_unknown(self):
        for value in (None,{},dict(complete=True,items=None)):
            self.assertEqual(classify(value,{}),('unknown',None))

    def test_plan_covers_two_objects_concurrent_and_unavailable(self):
        self.assertEqual(len(scenarios()),16)
        for kind in ('ticket','gitea'):
            self.assertEqual({p['fault'] for p in scenarios() if p['kind']==kind}&{'concurrent','unavailable'},{'concurrent','unavailable'})


class LedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.pg=PostgresRuntime(Path(cls.temp.name)/'postgres').start()
        cls.addClassCleanup(cls.pg.close)
        cls.store=Operations(cls.pg.dsn)
        cls.store.setup()

    def setUp(self):
        case=load_json(REPO_ROOT/'cases/tickets/full/T01.json')
        self.manifest=new_manifest(Path(self.temp.name)/('ledger-'+uuid4().hex),case)
        self.manifest['side_effect']=dict(kind='ticket',scope='tenant-A',operation_id=self.manifest['operation_id'],request=case['request'])
        self.store.create(self.manifest)
        self.lease=self.store.claim(self.manifest,str(uuid4()),30)
        self.store.prepare(self.manifest,self.lease)

    def test_prepared_request_is_durable_across_connections(self):
        self.assertEqual(Operations(self.pg.dsn).read(self.manifest)['state'],'prepared')

    def test_prepare_is_idempotent_and_does_not_reset_unknown(self):
        self.store.transition(self.manifest,self.lease,'sent_unknown',dict(reason='send'))
        self.assertEqual(self.store.prepare(self.manifest,self.lease)['state'],'sent_unknown')

    def test_manifest_content_change_rejected_before_write(self):
        foreign=deepcopy(self.manifest)
        foreign['side_effect']['request']['description']='changed'
        with self.assertRaises(BindingError):
            self.store.prepare(foreign,self.lease)
        self.assertEqual(self.store.read(self.manifest)['state'],'prepared')

    def test_direct_unleased_sql_cannot_write_ledger(self):
        with self.assertRaises(psycopg.errors.CheckViolation),self.store.connect() as conn:
            conn.execute("UPDATE ac_operations SET state='sent_unknown' WHERE job_id=%s",(self.manifest['job_id'],))

    def test_direct_unleased_sql_cannot_write_audit(self):
        with self.assertRaises(psycopg.errors.CheckViolation),self.store.connect() as conn:
            conn.execute("DELETE FROM ac_operation_events WHERE job_id=%s",(self.manifest['job_id'],))

    def test_foreign_owner_cannot_change_unknown(self):
        with self.assertRaises(LeaseLost):
            self.store.transition(self.manifest,self.lease|dict(owner=str(uuid4())),'sent_unknown',dict(reason='wrong'))

    def test_expired_generation_cannot_write_after_takeover(self):
        self.store.heartbeat(self.manifest,self.lease,1)
        wait_expiry(self.store,self.manifest)
        takeover=self.store.claim(self.manifest,str(uuid4()),30)
        with self.assertRaises(LeaseLost):
            self.store.transition(self.manifest,self.lease,'sent_unknown',dict(reason='stale'))
        self.store.transition(self.manifest,takeover,'sent_unknown',dict(reason='current'))

    def test_prepared_cannot_skip_to_confirmed(self):
        with self.assertRaises(BindingError):
            self.store.transition(self.manifest,self.lease,'confirmed',dict(reason='fabricated'),dict(id=1))

    def test_conflict_is_terminal_and_preserves_no_success_result(self):
        self.store.transition(self.manifest,self.lease,'sent_unknown',dict(reason='send'))
        self.store.transition(self.manifest,self.lease,'conflict',dict(reason='duplicate'))
        with self.assertRaises(BindingError):
            self.store.transition(self.manifest,self.lease,'sent_unknown',dict(reason='reset'))
        self.assertIsNone(self.store.read(self.manifest)['result'])

    def test_confirmed_result_is_returned_without_target_access(self):
        self.store.transition(self.manifest,self.lease,'sent_unknown',dict(reason='send'))
        self.store.transition(self.manifest,self.lease,'confirmed',dict(reason='observed'),dict(id=1))
        result=Recovery(self.store,self.manifest,self.lease,None).apply()
        self.assertEqual(result,dict(id=1))

    def test_gitea_empty_unknown_never_creates(self):
        self.store.transition(self.manifest,self.lease,'sent_unknown',dict(reason='send'))
        class Target:
            atomic_idempotency=False
            expected={}
            def observe(self):return dict(complete=True,items=[])
            def create(self):raise AssertionError('Unsafe create')
        with self.assertRaises(NeedsVerification):
            Recovery(self.store,self.manifest,self.lease,Target()).apply()
        self.assertEqual(self.store.read(self.manifest)['state'],'sent_unknown')

    def test_unavailable_atomic_target_does_not_retry_write(self):
        self.store.transition(self.manifest,self.lease,'sent_unknown',dict(reason='send'))
        class Target:
            atomic_idempotency=True
            expected={}
            def observe(self):return dict(complete=False,items=None,error='503')
            def create(self):raise AssertionError('Unexpected write during read outage')
        with self.assertRaises(NeedsVerification):
            Recovery(self.store,self.manifest,self.lease,Target()).apply()

    def test_gitea_cannot_enable_atomic_replay(self):
        self.store.transition(self.manifest,self.lease,'sent_unknown',dict(reason='send'))
        class Target:atomic_idempotency=False
        with self.assertRaises(BindingError):
            Recovery(self.store,self.manifest,self.lease,Target(),retry_unknown=True).apply()


class SideEffectProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.summary=suite(Path(cls.temp.name))
        if cls.summary['status']!='PASS':raise AssertionError(cls.summary)
        cls.directory=Path(cls.summary['summary_path']).parent
        cls.records={r['name']:r for r in cls.summary['scenarios']}

    def data(self,name,file):return load_json(Path(self.records[name]['run_dir'])/file)

    def test_full_offline_oracle_passes(self):
        checked=recheck(self.directory)
        self.assertEqual(checked['status'],'PASS',checked)

    def test_all_six_committed_ticket_recoveries_have_one_effect(self):
        for strategy in ('query','replay'):
            for n in range(1,4):
                self.assertEqual(self.records[f'ticket-{strategy}-{n}']['final_count'],1)

    def test_commit_before_checkpoint_remains_unknown(self):
        for name in ('ticket-query-1','gitea-confirmed'):
            value=self.data(name,'at-gate.json')
            self.assertEqual(value['ledger']['state'],'sent_unknown')
            self.assertEqual(value['checkpoint']['next'],['apply'])

    def test_saved_step_continues_finish(self):
        self.assertEqual(self.data('ticket-saved','at-gate.json')['checkpoint']['next'],['finish'])
        self.assertEqual(self.records['ticket-saved']['final_count'],1)

    def test_same_key_different_content_returns_real_409(self):
        self.assertEqual(self.data('ticket-query-1','content-conflict.json')['http_status'],409)

    def test_ticket_empty_unknown_uses_atomic_contract(self):
        self.assertEqual(self.records['ticket-unknown-empty']['final_ledger'],'confirmed')

    def test_gitea_empty_unknown_stops(self):
        self.assertEqual(self.records['gitea-empty']['final_ledger'],'sent_unknown')
        self.assertEqual(self.records['gitea-empty']['final_count'],0)

    def test_gitea_duplicate_is_conflict_not_success(self):
        self.assertEqual(self.records['gitea-duplicate']['final_ledger'],'conflict')
        self.assertEqual(self.records['gitea-duplicate']['final_count'],2)

    def test_two_real_read_outages_preserve_unknown_and_later_confirm(self):
        for name in ('ticket-unavailable','gitea-unavailable'):
            self.assertEqual(self.data(name,'after-recovery.json')['ledger']['state'],'sent_unknown')
            self.assertEqual(self.records[name]['later'][0]['result']['status'],'PASS')

    def test_concurrent_recovery_has_one_takeover_generation(self):
        for name in ('ticket-concurrent','gitea-concurrent'):
            results=self.records[name]['recovery']
            self.assertEqual(len(results),3)
            owners=[r for r in results if r['result'].get('lease')]
            self.assertEqual(len(owners),1)
            self.assertEqual(owners[0]['result']['lease']['generation'],2)

    def test_confirmed_repeat_uses_cached_result(self):
        for r in self.records.values():
            if r['later']:self.assertTrue(r['later'][-1]['result']['cached'])

    def test_sixteen_abrupt_kills_keep_same_service_and_pg(self):
        for name,r in self.records.items():
            self.assertNotEqual(r['killed']['exit_code'],0)
            a,b=self.data(name,'before-kill-services.json'),self.data(name,'after-recovery-services.json')
            self.assertEqual({k:v for k,v in a.items() if k!='at'},{k:v for k,v in b.items() if k!='at'})

    def negative(self,name,file,mutate):
        with tempfile.TemporaryDirectory() as tmp:
            copied=Path(shutil.copytree(self.directory,Path(tmp)/'copy'))
            source=Path(self.records[name]['run_dir']).relative_to(self.directory)
            path=copied/source/file
            value=load_json(path)
            mutate(value)
            save_json(path,value)
            self.assertEqual(recheck(copied)['status'],'ERROR')

    def test_edited_final_json_cannot_manufacture_success(self):
        self.negative('ticket-query-1','final.json',lambda x:x['tickets'].clear())

    def test_edited_pg_channel_cannot_hide_ledger_gap(self):
        self.negative('ticket-query-1','at-gate.json',lambda x:x['checkpoint']['values'].update(phase='completed'))

    def test_forged_kill_receipt_rejected(self):
        self.negative('ticket-query-1','crash/termination.json',lambda x:x.update(exit_code=0))

    def test_gitea_missing_detail_cannot_claim_complete(self):
        self.negative('gitea-confirmed','final.json',lambda x:x['data']['detail_evidence'].clear())

    def test_ledger_request_hash_tampering_rejected(self):
        self.negative('ticket-query-1','completed.json',lambda x:x['ledger'].update(request_sha256='0'*64))


if __name__=='__main__':unittest.main()

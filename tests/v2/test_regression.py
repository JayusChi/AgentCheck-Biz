"""D32 counterexamples and actual duplicate-producing HTTP/MCP release gates."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import uuid4
import xml.etree.ElementTree as ET

from agentcheck_biz.checks import load_json
from agentcheck_biz.network_acceptance.process import launch
from agentcheck_biz.network_acceptance.verify import hashes
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from agentcheck_biz.v2.manifest import build
from agentcheck_biz.v2.runner import allocate
from agentcheck_biz.regression.compare import compare_batches, contract, policy, align
from agentcheck_biz.regression.facts import business_invariants, denial_trace
from agentcheck_biz.regression.reports import markdown, junit
from agentcheck_biz.regression.__main__ import output_directory


def fixture():
    """Explicit unit data; never used as integration or raw-network evidence."""
    m=build(['network:C_fixed_loss_retry'])
    request=m['slots'][0]['case']['request']
    facts=dict(kind='ticket',request=request,resources=[dict(request,ticket_id='T-unit')],
        result=dict(ticket_id='T-unit'),completed=True,confirmed=True,recovery=True,observation_complete=True,
        denial=dict(denied=False,writes_after=[]),evidence=['unit-facts'],trace_evidence=['unit-trace'])
    return m,facts


def record(facts,raw='PASS',matched=True):
    return dict(state='completed',harness_matched=matched,identity={'run_id':'unit'},observed=dict(
        business_status=raw,evidence_status='PASS',coverage='covered',observation_status='complete'),
        facts=dict(invariants=business_invariants(facts),observer='unit-sqlite',prompt={'client':'unit-fixed/1'},
            runtime={'python':'unit'},resource_count=len(facts['resources']),conservative_pending=False))


def batch(m,r,directory):
    row=deepcopy(r);row['identity']={'run_id':directory}
    return dict(manifest=deepcopy(m),directory=directory,slots={'s001':row},error=None)


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.m,self.facts=fixture()
        self.a=batch(self.m,record(self.facts),'baseline-unit')
        self.b=batch(self.m,record(self.facts),'candidate-unit')

    def compare(self):return compare_batches(self.a,self.b)

    def changed(self,mutate,raw='FAIL'):
        facts=deepcopy(self.facts);mutate(facts)
        self.b['slots']['s001']=record(facts,raw)
        return self.compare()

    def test_valid_candidate_passes(self):self.assertEqual(self.compare()['status'],'PASS')

    def test_duplicate_is_new_violation_even_if_harness_matched(self):
        report=self.changed(lambda f:f['resources'].append(dict(f['resources'][0],ticket_id='T-duplicate')))
        self.assertEqual(report['status'],'FAIL')
        self.assertTrue(report['slots'][0]['candidate']['harness_matched'])
        self.assertTrue(next(i for i in report['slots'][0]['invariant_deltas'] if i['id']=='no_duplicate')['new_violation'])

    def test_wrong_customer_rejected(self):
        report=self.changed(lambda f:f['resources'][0].update(customer_id='C-wrong'))
        self.assertIn('correct_customer',report['slots'][0]['reason'])

    def test_wrong_returned_id_rejected(self):
        report=self.changed(lambda f:f.update(result={'ticket_id':'foreign'}))
        self.assertIn('correct_returned_id',report['slots'][0]['reason'])

    def test_write_after_permission_denial_rejected_even_if_business_raw_pass(self):
        report=self.changed(lambda f:f.update(denial=dict(denied=True,writes_after=['POST-2'])),'PASS')
        self.assertEqual(report['status'],'FAIL')
        self.assertIn('no_write_after_denial',report['slots'][0]['reason'])

    def test_unconfirmed_recovery_cannot_claim_completion(self):
        report=self.changed(lambda f:f.update(confirmed=False),'PASS')
        self.assertEqual(report['status'],'FAIL')
        self.assertIn('confirmed_before_completion',report['slots'][0]['reason'])

    def test_pending_may_not_return_invented_id(self):
        report=self.changed(lambda f:f.update(completed=False,confirmed=False),'INCONCLUSIVE')
        self.assertEqual(report['status'],'FAIL')

    def test_confirmed_tool_result_without_final_model_is_valid(self):
        report=self.changed(lambda f:f.update(completed=False),'PASS')
        self.assertEqual(report['status'],'PASS')

    def test_incomplete_observation_does_not_prove_empty_success(self):
        report=self.changed(lambda f:f.update(resources=[],result=None,completed=False,observation_complete=False),'INCONCLUSIVE')
        self.assertEqual(report['status'],'INCONCLUSIVE')

    def test_untriggered_denial_is_na_not_fault_coverage(self):
        inv=next(i for i in business_invariants(self.facts) if i['id']=='no_write_after_denial')
        self.assertEqual(inv['status'],'N/A')

    def test_gitea_has_no_customer_contract(self):
        f=deepcopy(self.facts);f.update(kind='gitea',request={'title':'unit'},resources=[{'id':1,'number':1,'title':'unit'}],result={'id':1,'number':1})
        inv=business_invariants(f)
        self.assertEqual(next(i['status'] for i in inv if i['id']=='correct_customer'),'N/A')

    def test_gitea_number_must_match_as_well_as_id(self):
        f=deepcopy(self.facts);f.update(kind='gitea',request={'title':'unit'},resources=[{'id':1,'number':1,'title':'unit'}],result={'id':1,'number':2})
        self.assertEqual(next(i['status'] for i in business_invariants(f) if i['id']=='correct_returned_id'),'FAIL')

    def test_harness_expected_fail_is_not_release_pass(self):
        self.b['slots']['s001']['observed']['business_status']='FAIL'
        self.assertEqual(compare_batches(None,self.b,suite='harness')['status'],'PASS')
        self.assertEqual(self.compare()['status'],'FAIL')

    def test_changing_selftest_expectation_does_not_change_release(self):
        before=self.compare()['status'];self.b['manifest']['slots'][0]['expected']['business_status']='FAIL'
        self.assertEqual(self.compare()['status'],before)

    def test_baseline_fail_candidate_fail_cannot_pass(self):
        for b in (self.a,self.b):b['slots']['s001']['observed']['business_status']='FAIL'
        self.assertEqual(self.compare()['status'],'FAIL')

    def test_fixed_candidate_can_improve_valid_failing_baseline(self):
        self.a['slots']['s001']['observed']['business_status']='FAIL'
        self.assertEqual(self.compare()['status'],'PASS')

    def test_error_preserved(self):
        self.b['slots']['s001']['observed']['business_status']='ERROR'
        self.assertEqual(self.compare()['status'],'ERROR')

    def test_inconclusive_preserved(self):
        self.b['slots']['s001']['observed']['business_status']='INCONCLUSIVE'
        self.assertEqual(self.compare()['status'],'INCONCLUSIVE')

    def test_missed_fault_remains_in_denominator(self):
        self.b['slots']['s001']['observed'].update(coverage='not_covered',evidence_status='INCONCLUSIVE')
        r=self.compare();self.assertEqual((r['status'],r['planned']),('INCONCLUSIVE',1))

    def test_failed_first_observation_not_hidden_by_final_pass(self):
        self.b['slots']['s001']['observed'].update(business_status='INCONCLUSIVE',observation_status='failed',final_business_status='PASS')
        self.assertEqual(self.compare()['status'],'INCONCLUSIVE')

    def test_not_started_slot_kept(self):
        self.b['slots']['s001']=dict(state='not_started',harness_matched=False)
        r=self.compare();self.assertEqual((r['status'],r['planned']),('INCONCLUSIVE',1))

    def test_error_slot_is_error(self):
        self.b['slots']['s001']=dict(state='error',harness_matched=False)
        self.assertEqual(self.compare()['status'],'ERROR')

    def test_missing_candidate_slot_kept(self):
        self.b['manifest']['slots']=[];self.b['slots']={}
        r=self.compare();self.assertEqual((r['status'],r['planned']),('INCONCLUSIVE',1))

    def test_extra_candidate_slot_kept(self):
        spec=deepcopy(self.m['slots'][0]);spec['slot_id']='s002';self.b['manifest']['slots'].append(spec)
        self.b['slots']['s002']=deepcopy(self.b['slots']['s001'])
        r=self.compare();self.assertEqual((r['status'],r['planned']),('INCONCLUSIVE',2))

    def test_zero_inventory_never_passes(self):
        for b in (self.a,self.b):b['manifest']['slots']=[];b['slots']={}
        self.assertEqual(self.compare()['status'],'INCONCLUSIVE')

    def test_duplicate_slot_ids_refused(self):
        self.b['manifest']['slots'].append(deepcopy(self.m['slots'][0]))
        with self.assertRaisesRegex(ValueError,'Duplicate'):self.compare()

    def test_reordered_inventory_blocks_release(self):
        for b in (self.a,self.b):
            s=deepcopy(b['manifest']['slots'][0]);s['slot_id']='s002';b['manifest']['slots'].append(s)
            b['slots']['s002']=deepcopy(b['slots']['s001'])
        self.b['manifest']['slots'].reverse()
        self.assertEqual(self.compare()['status'],'INCONCLUSIVE')

    def test_same_batch_not_an_independent_candidate(self):
        self.b['directory']=self.a['directory'];self.assertEqual(self.compare()['status'],'INCONCLUSIVE')

    def test_copy_of_baseline_in_another_directory_is_not_a_new_candidate(self):
        self.b['slots']['s001']['identity']=deepcopy(self.a['slots']['s001']['identity'])
        r=self.compare();self.assertEqual(r['status'],'INCONCLUSIVE')
        self.assertFalse(r['slots'][0]['alignment']['comparable'])

    def test_reused_identity_across_different_slot_ids_is_detected(self):
        spec=deepcopy(self.m['slots'][0]);spec['slot_id']='s002'
        self.a['manifest']['slots'].append(spec)
        self.a['slots']['s002']=deepcopy(self.a['slots']['s001'])
        self.a['slots']['s002']['identity']={'run_id':'different-slot-baseline'}
        self.b['slots']['s001']['identity']={'run_id':'different-slot-baseline'}
        self.assertIn('s001',self.compare()['controls'][-1]['slots'])

    def test_each_required_control_is_compared(self):
        for field in ('case','initial_state','fault','budget','recovery','adapter','engine'):
            with self.subTest(field=field):
                b=deepcopy(self.b)
                if field=='case':b['manifest']['slots'][0][field]['task']='Changed task'
                else:b['manifest']['slots'][0][field]={'unit-change':True}
                r=compare_batches(self.a,b);self.assertEqual(r['status'],'INCONCLUSIVE')
                self.assertIn(field,[d['field'] for d in r['slots'][0]['alignment']['differences']])

    def test_cross_object_comparison_blocked(self):
        self.b['manifest']['slots'][0]['object']['kind']='gitea'
        self.assertEqual(self.compare()['status'],'INCONCLUSIVE')

    def test_prompt_observer_runtime_independently_compared(self):
        for field in ('prompt','observer','runtime'):
            with self.subTest(field=field):
                b=deepcopy(self.b);b['slots']['s001']['facts'][field]='different'
                self.assertEqual(compare_batches(self.a,b)['status'],'INCONCLUSIVE')

    def test_missing_provenance_blocks_attribution(self):
        self.b['slots']['s001']['facts'].pop('prompt')
        self.assertEqual(self.compare()['slots'][0]['alignment']['attribution'],'blocked')

    def test_only_service_change_allows_bounded_single_factor_label(self):
        self.b['manifest']['slots'][0]['object']['version']='unsafe'
        self.assertEqual(self.compare()['slots'][0]['alignment']['attribution'],'service_version_only')

    def test_multi_change_prevents_single_factor_attribution(self):
        self.b['manifest']['slots'][0]['object']['version']='unsafe'
        self.b['manifest']['source_sha256']='0'*64
        self.assertEqual(self.compare()['slots'][0]['alignment']['attribution'],'blocked')

    def test_incomparable_delta_does_not_claim_new_violation(self):
        self.b['manifest']['slots'][0]['fault']={'unit-change':True}
        r=self.changed(lambda f:f['resources'].append(dict(f['resources'][0],ticket_id='second')))
        self.assertTrue(all(i['new_violation'] is None for i in r['slots'][0]['invariant_deltas']))

    def test_policy_cannot_whitelist_fail_or_error(self):
        for rule in ({'allow':'FAIL'},{'profile':'network:B_unsafe_loss_retry','case_id':'T01_normal_create','rule':'pending-without-replay/1','reason':'ignore bug'}):
            with self.assertRaises(ValueError):policy(dict(schema_version='agentcheck-release-policy/1',case_exceptions=[rule]))

    def test_pending_exception_requires_exact_case_and_raw_proof(self):
        spec=build(['continuity:gitea-unknown-empty'])['slots'][0]
        p=policy(dict(schema_version='agentcheck-release-policy/1',case_exceptions=[dict(profile=spec['profile'],case_id='G01_create',rule='pending-without-replay/1',reason='人工确认前保持等待，禁止重放写入')]))
        r=deepcopy(self.b['slots']['s001']);r['observed']['business_status']='INCONCLUSIVE'
        self.assertEqual(contract(spec,r,p)['status'],'INCONCLUSIVE')
        r['facts']['conservative_pending']=True
        self.assertEqual(contract(spec,r,p)['status'],'PASS')
        r['observed']['business_status']='FAIL'
        self.assertEqual(contract(spec,r,p)['status'],'FAIL')

    def test_harness_cannot_compare_baseline(self):
        with self.assertRaises(ValueError):compare_batches(self.a,self.b,suite='harness')

    def test_release_requires_baseline(self):
        with self.assertRaises(ValueError):compare_batches(None,self.b)

    def test_junit_has_failure_error_skip_and_raw_states(self):
        r=self.compare();original=deepcopy(r['slots'][0]);r['slots']=[]
        for state in ('PASS','FAIL','ERROR','INCONCLUSIVE'):
            row=deepcopy(original);row.update(slot_id=state,status=state);row['candidate']['observed']['business_status']=state;r['slots'].append(row)
        root=ET.fromstring(junit(r))
        self.assertEqual([root.get(k) for k in ('tests','failures','errors','skipped')],['4','1','1','1'])
        self.assertEqual([n.get('value') for n in root.findall('./testcase/properties/property[@name="candidate.business_status"]')],['PASS','FAIL','ERROR','INCONCLUSIVE'])

    def test_expected_harness_fail_export_is_explicitly_harness(self):
        self.b['slots']['s001']['observed']['business_status']='FAIL'
        r=compare_batches(None,self.b,suite='harness');root=ET.fromstring(junit(r))
        self.assertEqual(root.get('name'),'harness_selftest_only');self.assertEqual(root.get('failures'),'0')
        self.assertIn('FAIL',ET.tostring(root,encoding='unicode'))

    def test_markdown_and_xml_escape_untrusted_values(self):
        r=self.compare();r['slots'][0]['reason']='<script>& | [link](url)\n\x01'
        self.assertNotIn('<script>',markdown(r));self.assertIn('\\|',markdown(r));ET.fromstring(junit(r))

    def test_reports_do_not_hide_control_only_failure(self):
        r=self.compare();r['controls']=[dict(status='ERROR',reason='bad manifest')]
        self.assertEqual(ET.fromstring(junit(r)).get('errors'),'1')

    def test_report_output_cannot_overwrite_input(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT/'tmp') as tmp:
            root=Path(tmp);evidence=root/'evidence';evidence.mkdir()
            for path in (evidence,evidence/'report',root):
                with self.assertRaises(ValueError):output_directory(path,[evidence])

    def test_existing_output_refused(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT/'tmp') as tmp:
            with self.assertRaises(FileExistsError):output_directory(tmp,[Path(tmp).parent/'unit-input'])

    def test_denial_trace_orders_across_workers(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT/'tmp') as tmp:
            a=Path(tmp)/'first.jsonl';b=Path(tmp)/'second.jsonl'
            save=lambda p,v:p.write_text(json.dumps(v)+'\n',encoding='utf8')
            save(a,dict(seq=2,event='http_response_received',time_utc='2026-01-01T00:00:00+00:00',status_code=403))
            save(b,dict(seq=1,event='http_request_started',time_utc='2026-01-01T00:00:01+00:00',method='POST',attempt_id='next-worker'))
            self.assertEqual(denial_trace([b,a]),dict(denied=True,writes_after=['next-worker']))


class RegressionExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(os.environ.get('AGENTCHECK_D32_EVIDENCE',str(REPO_ROOT/'artifacts/v2'/('d32-tests-'+uuid4().hex))))
        cls.root.mkdir(parents=True,exist_ok=True);cls.runs={};cls.reports={}
        for label,profile in [('baseline','network:C_fixed_loss_retry'),('fixed','network:C_fixed_loss_retry'),
                              ('bug','network:B_unsafe_loss_retry'),('pending-baseline','continuity:gitea-unknown-empty'),
                              ('pending','continuity:gitea-unknown-empty')]:
            m=build([profile]);path=cls.root/(label+'-manifest.json');save_json(path,m)
            receipt=launch(['-m','agentcheck_biz.v2','run','--manifest',str(path),'--output',str(cls.root/label)],cls.root/(label+'.log'),timeout=300)
            save_json(cls.root/(label+'-process.json'),receipt)
            if receipt['exit_code']!=0:raise AssertionError('Native fixture failed: '+receipt['log'])
            result=json.loads(Path(receipt['log']).read_text(encoding='utf8').splitlines()[-1])
            cls.runs[label]=(path,Path(result['directory']))
        for label,command,expected in [('fixed','compare',0),('bug','compare',1),('bug','selftest',0)]:
            cls.run_report(label+'-'+command,label,command,expected)
        p=cls.root/'pending-policy.json'
        save_json(p,policy(dict(schema_version='agentcheck-release-policy/1',case_exceptions=[dict(
            profile='continuity:gitea-unknown-empty',case_id='G01_create',rule='pending-without-replay/1',
            reason='该案例要求业务仍未知时保持等待人工确认，禁止再次写入或宣告完成')])))
        cls.run_report('pending-strict','pending','compare',2,baseline_label='pending-baseline')
        cls.run_report('pending-policy','pending','compare',0,['--policy',str(p)],baseline_label='pending-baseline')

    @classmethod
    def run_report(cls,name,label,command,expected,extra=None,baseline_label='baseline'):
        mp,directory=cls.runs[label];out=cls.root/name
        args=['-m','agentcheck_biz.regression',command,'--candidate-manifest',str(mp),'--candidate',str(directory),'--output',str(out)]
        roots=[directory]
        if command=='compare':
            bp,bd=cls.runs[baseline_label];args += ['--baseline-manifest',str(bp),'--baseline',str(bd)];roots.append(bd)
        if extra:args+=extra
        before=[hashes(p) for p in roots]
        receipt=launch(args,cls.root/(name+'.log'),timeout=180)
        save_json(cls.root/(name+'-process.json'),receipt)
        if receipt['exit_code']!=expected:raise AssertionError(f'Expected {expected}: '+Path(receipt['log']).read_text(encoding='utf8'))
        if before!=[hashes(p) for p in roots]:raise AssertionError('Comparison mutated raw evidence')
        report=load_json(out/'report.json');cls.reports[name]=report
        return report

    def test_actual_duplicate_blocks_release(self):
        r=self.reports['bug-compare'];self.assertEqual(r['status'],'FAIL')
        row=r['slots'][0];self.assertTrue(row['candidate']['harness_matched'])
        self.assertEqual(row['candidate']['facts']['resource_count'],2)
        self.assertTrue(next(i for i in row['invariant_deltas'] if i['id']=='no_duplicate')['new_violation'])
        self.assertEqual(row['alignment']['attribution'],'service_version_only')

    def test_actual_fixed_candidate_passes(self):
        r=self.reports['fixed-compare'];self.assertEqual(r['status'],'PASS')
        self.assertEqual(r['slots'][0]['candidate']['facts']['resource_count'],1)

    def test_actual_harness_pass_cannot_hide_release_failure(self):
        r=self.reports['bug-selftest'];self.assertEqual(r['status'],'PASS')
        self.assertEqual(r['purpose'],'harness_selftest_only')
        self.assertEqual(r['slots'][0]['candidate']['contract']['status'],'FAIL')

    def test_three_formats_retain_actual_failure(self):
        out=self.root/'bug-compare'
        self.assertIn('no\\_duplicate',(out/'report.md').read_text(encoding='utf8'))
        x=ET.parse(out/'junit.xml').getroot();self.assertEqual(x.get('failures'),'1')
        self.assertEqual(x.find('./testcase/properties/property[@name="candidate.business_status"]').get('value'),'FAIL')

    def test_report_invariant_references_exist(self):
        for report in self.reports.values():
            for slot in report['slots']:
                for side in ('baseline','candidate'):
                    facts=((slot.get(side) or {}).get('facts') or {})
                    for inv in facts.get('invariants',[]):
                        for ref in inv['evidence']:
                            self.assertTrue((Path(facts['raw_evidence'])/ref.split(':',1)[0]).is_file(),ref)

    def test_actual_pending_requires_explicit_case_policy(self):
        self.assertEqual(self.reports['pending-strict']['status'],'INCONCLUSIVE')
        r=self.reports['pending-policy'];self.assertEqual(r['status'],'PASS')
        row=r['slots'][0];self.assertEqual(row['candidate']['observed']['business_status'],'INCONCLUSIVE')
        self.assertEqual(row['candidate']['contract']['exception']['rule'],'pending-without-replay/1')
        x=ET.parse(self.root/'pending-policy/junit.xml').getroot()
        self.assertEqual(x.find('./testcase/properties/property[@name="candidate.business_status"]').get('value'),'INCONCLUSIVE')

    def test_fresh_independent_readonly_oracle(self):
        for r in self.reports.values():
            self.assertNotEqual(r['verifier_pid'],os.getpid());self.assertTrue(r['evidence_unchanged']);self.assertTrue(r['read_only'])
        ids=[load_json(self.runs[label][1]/'summary.json')['slots'][0]['inspection']['identity']['run_id']
             for label in ('baseline','fixed','bug','pending-baseline','pending')]
        self.assertEqual(len(ids),len(set(ids)))

    def test_tampered_raw_evidence_is_error_not_expected_fail(self):
        mp,directory=self.runs['bug'];target=self.root/'tampered-input';shutil.copytree(directory,target)
        p=next(target.rglob('final.json'));data=load_json(p);data['tickets']=[];save_json(p,data)
        self.runs['tampered']=(mp,target)
        r=self.run_report('tampered-compare','tampered','compare',3)
        self.assertEqual(r['status'],'ERROR');self.assertEqual(r['planned'],1)

    def test_copied_baseline_artifacts_cannot_pass_release(self):
        mp,directory=self.runs['baseline'];target=self.root/'copied-baseline-input';shutil.copytree(directory,target)
        self.runs['copied']=(mp,target)
        r=self.run_report('copied-baseline-compare','copied','compare',2)
        self.assertEqual(r['status'],'INCONCLUSIVE')
        self.assertIn('run_id',r['slots'][0]['alignment']['reused_identities'])

    def test_partial_slot_is_skipped_with_nonzero_exit(self):
        mp,_=self.runs['fixed'];m=load_json(mp);directory,_=allocate(m,self.root/'partial-input')
        self.runs['partial']=(mp,directory)
        r=self.run_report('partial-compare','partial','compare',2)
        self.assertEqual(r['planned'],1);self.assertEqual(r['status'],'INCONCLUSIVE')
        self.assertEqual(ET.parse(self.root/'partial-compare/junit.xml').getroot().get('skipped'),'1')

    def test_unapproved_exception_policy_fails_closed(self):
        p=self.root/'bad-policy.json';save_json(p,dict(schema_version='agentcheck-release-policy/1',case_exceptions=[{'allow':'FAIL'}]))
        r=self.run_report('policy-error','bug','compare',3,['--policy',str(p)])
        self.assertEqual(r['status'],'ERROR')


if __name__=='__main__':unittest.main()

"""D28 real Agent kills, fixed-service recovery contrast and evidence negatives."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import uuid4

from agentcheck_biz.checks import load_json
from agentcheck_biz.reports import save_json
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.persistence.store import new_manifest, IDENTITY_KEYS, BindingError, digest
from agentcheck_biz.agent_crash.demo import scenarios, suite
from agentcheck_biz.agent_crash.worker import validate_spec, matches_ticket
from agentcheck_biz.agent_crash.verify import recheck


class CrashSpecificationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = new_manifest(Path(tempfile.gettempdir()) / ('spec-' + uuid4().hex),
            load_json(REPO_ROOT / 'cases/tickets/full/T01.json'))
        self.spec = {k: self.manifest[k] for k in IDENTITY_KEYS} | dict(protocol='agent-crash/1', test_only=True,
            window='after_commit', strategy='query_first', damage='none', service_version='unsafe', model_requests=0)

    def test_test_flag_required(self):
        for value in (False, None, 'true', 1):
            with self.subTest(value=value), self.assertRaises(BindingError):
                validate_spec({**self.spec, 'test_only': value}, self.manifest)

    def test_foreign_experiment_thread_operation_rejected(self):
        for key in ('job_id', 'thread_id', 'operation_id', 'environment_id', 'request_sha256'):
            with self.subTest(key=key), self.assertRaises(BindingError):
                validate_spec({**self.spec, key: str(uuid4())}, self.manifest)

    def test_unsupported_window_strategy_service_or_protocol_rejected(self):
        for key in ('window', 'strategy', 'service_version', 'protocol', 'damage'):
            with self.subTest(key=key), self.assertRaises(BindingError):
                validate_spec({**self.spec, key: 'unknown'}, self.manifest)

    def test_model_calls_cannot_be_enabled_in_fixed_comparison(self):
        with self.assertRaises(BindingError):
            validate_spec({**self.spec, 'model_requests': 1}, self.manifest)

    def test_plan_has_three_pairs_and_all_crash_windows(self):
        plan = scenarios()
        self.assertEqual(len(plan), 10)
        self.assertEqual({p['window'] for p in plan}, {'before_call', 'after_commit', 'after_checkpoint'})
        for strategy in ('replay', 'query_first'):
            self.assertEqual([p['repeat'] for p in plan if p['window'] == 'after_commit' and p['strategy'] == strategy], [1, 2, 3])

    def test_queried_ticket_must_match_whole_bound_request(self):
        ticket = self.manifest['case']['context'] | self.manifest['case']['request'] | dict(ticket_id='T-fixture', status='open')
        self.assertTrue(matches_ticket(ticket, self.manifest))
        for key in ticket:
            with self.subTest(key=key):
                self.assertFalse(matches_ticket({**ticket, key: None}, self.manifest))


class AgentCrashProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.result = suite(Path(cls.temp.name))
        if cls.result['status'] != 'PASS':
            raise AssertionError(cls.result)
        cls.directory = Path(cls.result['summary_path']).parent
        cls.records = {r['name']: r for r in cls.result['scenarios']}

    def data(self, name, file):
        return load_json(Path(self.records[name]['run_dir']) / file)

    def test_ten_real_worker_kills_and_twenty_distinct_processes(self):
        pids = []
        for record in self.records.values():
            self.assertEqual(record['killed']['termination'], 'owned_Popen.kill')
            self.assertNotEqual(record['killed']['exit_code'], 0)
            self.assertTrue(record['killed']['exited'] and record['killed']['result_absent'])
            pids += [record['killed']['pid'], record['recovery']['pid']]
        self.assertEqual(len(set(pids)), 20)

    def test_before_call_has_no_effect_at_gate_and_one_after_recovery(self):
        checkpoint = self.data('before-call', 'at-gate-checkpoint.json')['checkpoint']
        self.assertEqual(checkpoint['values']['phase'], 'prepared')
        state = self.data('before-call', 'at-gate-business.json')
        self.assertFalse(any(t['tenant_id'] == 'tenant-A' for t in state['tickets']))
        self.assertEqual(self.records['before-call']['expected'], 'PASS')

    def test_commit_before_checkpoint_proved_by_independent_business_read(self):
        for strategy in ('replay', 'query_first'):
            name = f'commit-{strategy}-1'
            cp = self.data(name, 'at-gate-checkpoint.json')['checkpoint']
            state = self.data(name, 'at-gate-business.json')
            self.assertIsNone(cp['values']['ticket'])
            self.assertEqual(cp['values']['create_calls'], 0)
            self.assertEqual(len([r for r in state['tickets'] if r['tenant_id'] == 'tenant-A']), 1)

    def test_unsafe_replay_exposes_duplicate_three_times(self):
        for repeat in range(1, 4):
            record = self.records[f'commit-replay-{repeat}']
            self.assertEqual((record['expected'], record['resource_count']), ('FAIL', 2))
            self.assertEqual(record['recovery']['result']['job_state'], 'waiting_verification')

    def test_query_first_fixes_same_unsafe_service_three_times(self):
        for repeat in range(1, 4):
            record = self.records[f'commit-query_first-{repeat}']
            self.assertEqual((record['expected'], record['resource_count']), ('PASS', 1))
            self.assertEqual(record['recovery']['result']['job_state'], 'finished')
            self.assertEqual(record['spec']['service_version'], 'unsafe')

    def test_only_recovery_strategy_changes_between_pairs(self):
        for repeat in range(1, 4):
            one, two = [self.records[f'commit-{s}-{repeat}']['spec'] for s in ('replay', 'query_first')]
            for key in ('request_sha256', 'service_version', 'graph_source_sha256', 'prompt', 'window', 'damage', 'lease_seconds'):
                self.assertEqual(one[key], two[key], key)

    def test_same_thread_operation_new_attempt_after_kill(self):
        for record in self.records.values():
            barrier = load_json(Path(record['run_dir']) / 'crash/barrier.json')
            lease = record['recovery']['result']['lease']
            self.assertEqual(barrier['thread_id'], record['manifest']['thread_id'])
            self.assertEqual(barrier['operation_id'], record['manifest']['operation_id'])
            self.assertEqual(lease['job_id'], barrier['job_id'])
            self.assertNotEqual(lease['attempt_id'], barrier['lease']['attempt_id'])
            self.assertEqual(lease['generation'], 2)

    def test_postgres_and_http_survive_each_agent_kill(self):
        for name in self.records:
            before = self.data(name, 'before-kill-services.json')
            for label in ('after-kill', 'after-recovery'):
                after = self.data(name, label + '-services.json')
                for key in ('pg_pid', 'pg_system_identifier', 'pg_started_at', 'http_pid', 'http_identity'):
                    self.assertEqual(before[key], after[key])

    def test_saved_checkpoint_does_not_rerun_create(self):
        name = 'checkpoint-saved'
        cp = self.data(name, 'at-gate-checkpoint.json')['checkpoint']
        self.assertEqual(cp['next'], ['verify'])
        self.assertEqual(cp['values']['create_calls'], 1)
        self.assertEqual(self.records[name]['resource_count'], 1)
        requests = [json.loads(line) for line in (Path(self.records[name]['run_dir']) / 'recover/http-client.jsonl').read_text(encoding='utf8').splitlines()]
        self.assertTrue(all(r.get('method') != 'POST' for r in requests))

    def test_query_repair_is_a_real_persisted_checkpoint(self):
        name = 'commit-query_first-1'
        reconciled = self.data(name, 'recover/checkpoint-reconciled.json')
        self.assertEqual(reconciled['sha256'], digest(reconciled['checkpoint']))
        self.assertEqual(reconciled['checkpoint']['values']['phase'], 'effect_observed')
        self.assertNotEqual(reconciled['checkpoint']['checkpoint_id'], self.data(name, 'before-recovery-checkpoint.json')['checkpoint']['checkpoint_id'])

    def test_missing_and_corrupt_checkpoints_require_manual_verification(self):
        for name in ('checkpoint-missing', 'checkpoint-corrupt'):
            result = self.records[name]['recovery']['result']
            self.assertEqual(result['status'], 'ERROR')
            self.assertIn('manual verification', result['reason'].lower())
            self.assertEqual(result['job_state'], 'waiting_verification')
            path = Path(self.records[name]['run_dir']) / 'recover/http-client.jsonl'
            requests = [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []
            self.assertFalse(any(r['event'] == 'http_request_started' for r in requests))

    def test_offline_recheck_agrees_with_saved_business_truth(self):
        result = recheck(self.directory)
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['business_outcomes'], dict(PASS=5, FAIL=3, ERROR=2))

    def negative(self, mutate):
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(shutil.copytree(self.directory, Path(temp) / 'copy'))
            summary = load_json(copied / 'summary.json')
            record = summary['scenarios'][0]
            run = copied / Path(record['run_dir']).relative_to(self.directory)
            mutate(copied, run, summary, record)
            save_json(copied / 'summary.json', summary)
            self.assertEqual(recheck(copied)['status'], 'ERROR')

    def test_missing_termination_receipt_cannot_pass(self):
        self.negative(lambda copied, run, summary, record: (run / 'crash/termination.json').unlink())

    def test_forged_killed_pid_cannot_pass_even_when_summary_matches(self):
        def mutate(copied, run, summary, record):
            killed = load_json(run / 'crash/termination.json')
            killed['pid'] = killed['controller_pid']
            record['killed'] = killed
            save_json(run / 'crash/termination.json', killed)
        self.negative(mutate)

    def test_changed_checkpoint_digest_cannot_hide_pg_mismatch(self):
        def mutate(copied, run, summary, record):
            saved = load_json(run / 'at-gate-checkpoint.json')
            saved['checkpoint']['values']['create_calls'] = 99
            saved['sha256'] = digest(saved['checkpoint'])
            save_json(run / 'at-gate-checkpoint.json', saved)
        self.negative(mutate)

    def test_edited_final_json_cannot_manufacture_duplicate_failure(self):
        def mutate(copied, run, summary, record):
            final = load_json(run / 'final.json')
            final['tickets'] = []
            save_json(run / 'final.json', final)
        self.negative(mutate)

    def test_removed_health_exchange_cannot_claim_service_survived(self):
        def mutate(copied, run, summary, record):
            path = run / 'http-service-events.jsonl'
            records = [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()]
            records = [r for r in records if r.get('request_id') != 'controller-after-kill']
            path.write_text('\n'.join(json.dumps(r) for r in records), encoding='utf8')
        self.negative(mutate)

    def test_missing_pg_capture_prevents_checkpoint_proof(self):
        self.negative(lambda copied, run, summary, record: (run / 'at-gate-postgres.json').unlink())

    def test_missing_scenario_cannot_shrink_comparison_denominator(self):
        self.negative(lambda copied, run, summary, record: summary['scenarios'].pop())

    def test_zero_models_and_owned_services_closed(self):
        self.assertEqual(self.result['model_requests'], 0)
        self.assertTrue(all(r['exited'] for r in self.result['cleanup']))
        self.assertTrue(all(r['recovery']['exited'] for r in self.records.values()))


if __name__ == '__main__':
    unittest.main()

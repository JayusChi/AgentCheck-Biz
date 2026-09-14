"""D27 real PostgreSQL races, expiry, takeover and database-level fencing."""

from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from agentcheck_biz.checks import load_json
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from agentcheck_biz.persistence.runtime import PostgresRuntime
from agentcheck_biz.persistence.store import Store, BindingError, new_manifest, IDENTITY_KEYS, digest
from agentcheck_biz.persistence.graph import build, config
from agentcheck_biz.scheduling.store import Scheduler, LeaseLost
from agentcheck_biz.scheduling.demo import suite
from agentcheck_biz.scheduling.verify import recheck, timestamp


class LeaseTimestampTests(unittest.TestCase):
    def test_postgres_variable_precision_and_timezone_compare_correctly(self):
        self.assertEqual(timestamp('2026-09-10T14:48:50.32388+08:00'), timestamp('2026-09-10T06:48:50.323880+00:00'))
        self.assertEqual(timestamp('2026-09-10T14:48:50.3+08:00').microsecond, 300000)
        self.assertEqual(timestamp('2026-09-10T14:48:50+08:00').microsecond, 0)

    def test_naive_timestamp_is_not_accepted_as_a_lease_deadline(self):
        with self.assertRaises(ValueError):
            timestamp('2026-09-10T14:48:50.123456')


class WorkerProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.result = suite(Path(cls.temp.name))
        if cls.result['status'] != 'PASS':
            raise AssertionError(cls.result)
        cls.directory = Path(cls.result['summary_path']).parent
        cls.steps = {name: {s['label']: s['result'] for s in cls.result[name]['steps']} for name in ('race', 'zombie', 'gap')}

    def test_real_process_race_has_exactly_one_valid_owner(self):
        race = self.steps['race']
        self.assertEqual(sorted(race['racer-' + str(i)]['status'] for i in range(2)), ['BUSY', 'PASS'])
        self.assertNotEqual(race['racer-0']['pid'], race['racer-1']['pid'])

    def test_renewal_blocks_other_worker(self):
        self.assertEqual(self.steps['race']['heartbeat']['status'], 'PASS')
        self.assertEqual(self.steps['race']['during-renewed-lease']['status'], 'BUSY')

    def test_expiry_enters_waiting_verification_in_all_scenarios(self):
        for name in self.steps:
            state = load_json(Path(self.result[name]['run_dir']) / 'expired.json')
            self.assertEqual(state['state'], 'waiting_verification')

    def test_takeover_increments_generation_and_changes_attempt(self):
        old, new = self.steps['zombie']['old-worker']['lease'], self.steps['zombie']['takeover']['lease']
        self.assertEqual((old['generation'], new['generation']), (1, 2))
        self.assertNotEqual(old['owner'], new['owner'])
        self.assertNotEqual(old['attempt_id'], new['attempt_id'])
        self.assertEqual(old['job_id'], new['job_id'])

    def test_live_zombie_is_fenced_on_all_six_write_paths(self):
        old = self.steps['zombie']['old-worker']
        self.assertEqual(len(old['probes']), 6)
        self.assertEqual(set(old['probes'].values()), {'REJECTED'})
        self.assertTrue(old['stale_writes_unchanged'])

    def test_takeover_checks_business_and_checkpoint_before_resume(self):
        result = self.steps['zombie']['takeover']
        self.assertTrue(result['reconciliation']['safe_to_resume'])
        self.assertEqual(result['reconciliation']['checkpoint']['values']['phase'], 'effect_observed')
        self.assertEqual(result['reconciliation']['business_tickets'], [result['values']['ticket']])
        self.assertEqual(result['job_state'], 'finished')
        self.assertEqual((result['values']['create_calls'], result['values']['query_calls']), (1, 1))

    def test_no_checkpoint_never_becomes_automatic_new_write(self):
        result = self.steps['race']['takeover']
        self.assertIsNone(result['reconciliation']['checkpoint'])
        self.assertEqual(result['reconciliation']['business_tickets'], [])
        self.assertEqual(result['status'], 'INCONCLUSIVE')

    def test_business_commit_checkpoint_gap_stays_waiting(self):
        result = self.steps['gap']['takeover']
        self.assertEqual(result['reconciliation']['checkpoint']['values']['phase'], 'prepared')
        self.assertEqual(len(result['reconciliation']['business_tickets']), 1)
        self.assertFalse(result['reconciliation']['safe_to_resume'])
        self.assertEqual(result['job_state'], 'waiting_verification')

    def test_offline_evidence_rechecks_without_live_services(self):
        result = recheck(self.directory)
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['worker_processes'], 9)

    def negative(self, mutate):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(shutil.copytree(self.directory, Path(temp) / 'copy'))
            mutate(copy)
            result = recheck(copy)
            self.assertEqual(result['status'], 'ERROR', result)

    def test_missing_lease_event_rejected_even_with_updated_digest(self):
        def mutate(copy):
            database = load_json(copy / 'postgres-snapshot.json')
            database['ac_lease_events'] = [r for r in database['ac_lease_events'] if r['kind'] != 'expired']
            summary = load_json(copy / 'summary.json')
            summary['database_snapshot_sha256'] = digest(database)
            save_json(copy / 'postgres-snapshot.json', database)
            save_json(copy / 'summary.json', summary)
        self.negative(mutate)

    def test_forged_generation_rejected_even_with_updated_digest(self):
        def mutate(copy):
            database = load_json(copy / 'postgres-snapshot.json')
            database['ac_leases'][0]['generation'] = 99
            summary = load_json(copy / 'summary.json')
            summary['database_snapshot_sha256'] = digest(database)
            save_json(copy / 'postgres-snapshot.json', database)
            save_json(copy / 'summary.json', summary)
        self.negative(mutate)

    def test_missing_business_query_evidence_rejected(self):
        def mutate(copy):
            original = Path(self.result['zombie']['run_dir']) / 'takeover/http-client.jsonl'
            (copy / original.relative_to(self.directory)).unlink()
        self.negative(mutate)

    def test_corrupt_checkpoint_cannot_be_hidden_by_export_hash(self):
        def mutate(copy):
            database = load_json(copy / 'postgres-snapshot.json')
            row = next(r for r in database['checkpoint_blobs'] if r['channel'] == 'ticket')
            row['blob'] = '\\x80'
            summary = load_json(copy / 'summary.json')
            summary['database_snapshot_sha256'] = digest(database)
            save_json(copy / 'postgres-snapshot.json', database)
            save_json(copy / 'summary.json', summary)
        self.negative(mutate)

    def test_zero_models_and_owned_process_cleanup(self):
        self.assertEqual(self.result['model_requests'], 0)
        self.assertTrue(all(r['exited'] for r in self.result['cleanup']))
        self.assertTrue(all(self.result['assertions'].values()))


class LeaseStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.pg = PostgresRuntime(Path(cls.temp.name)).start()
        cls.addClassCleanup(cls.pg.close)
        cls.store = Scheduler(cls.pg.dsn)
        cls.store.setup()

    def make(self):
        manifest = new_manifest(Path(self.temp.name) / ('job-' + uuid4().hex), load_json(REPO_ROOT / 'cases/tickets/full/T01.json'))
        self.store.create(manifest)
        return manifest

    def claim(self, manifest):
        return self.store.claim(manifest, str(uuid4()), 30)

    def age(self, manifest):
        # Unit-test clock fixture only; acceptance waits for real lease expiry.
        with self.store.connect() as conn:
            conn.execute("UPDATE ac_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=%s", (manifest['job_id'],))

    def test_queue_membership_is_durable_and_initial_generation_zero(self):
        manifest = self.make()
        row = next(r for r in self.store.export()['ac_leases'] if r['job_id'] == manifest['job_id'])
        self.assertEqual(row['generation'], 0)
        self.assertIsNone(row['owner'])
        self.assertEqual(self.store.get(manifest)['state'], 'queued')

    def test_setup_idempotent_and_d26_setup_still_works(self):
        before = self.store.export()
        self.store.setup()
        Store(self.pg.dsn).setup()
        self.assertEqual(before, self.store.export())

    def test_modified_migration_rejected(self):
        with self.store.connect() as conn:
            checksum = conn.execute('SELECT sha256 FROM ac_scheduler_versions').fetchone()['sha256']
            conn.execute("UPDATE ac_scheduler_versions SET sha256='modified'")
        try:
            with self.assertRaises(BindingError):
                self.store.setup()
        finally:
            with self.store.connect() as conn:
                conn.execute('UPDATE ac_scheduler_versions SET sha256=%s', (checksum,))

    def test_invalid_ttl_and_owner_do_not_mutate(self):
        manifest = self.make()
        before = self.store.export()
        for ttl in (0, -1, 301, 1.5, True):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.store.claim(manifest, str(uuid4()), ttl)
        with self.assertRaises(ValueError):
            self.store.claim(manifest, 'invalid')
        self.assertEqual(before, self.store.export())

    def test_foreign_manifest_refused_before_claim(self):
        manifest = self.make()
        manifest['thread_id'] = str(uuid4())
        with self.assertRaises(BindingError):
            self.claim(manifest)

    def test_expired_lease_cannot_be_renewed_without_reaper(self):
        manifest = self.make()
        lease = self.claim(manifest)
        self.age(manifest)
        with self.assertRaises(LeaseLost):
            self.store.heartbeat(manifest, lease)
        self.assertEqual(self.store.get(manifest)['state'], 'running')

    def test_expiration_is_idempotent_and_never_requeues(self):
        manifest = self.make()
        self.claim(manifest)
        self.assertFalse(self.store.expire(manifest))
        self.age(manifest)
        self.assertTrue(self.store.expire(manifest))
        self.assertFalse(self.store.expire(manifest))
        self.assertEqual(self.store.get(manifest)['state'], 'waiting_verification')

    def test_claim_reaps_expired_owner_and_monotonically_increments(self):
        manifest = self.make()
        one = self.claim(manifest)
        self.age(manifest)
        two = self.claim(manifest)
        self.assertEqual(two['generation'], one['generation'] + 1)
        self.assertTrue(two['takeover'])
        with self.assertRaises(LeaseLost):
            self.store.finish(manifest, one, 'finished', None, 'stale')

    def test_forged_token_fields_rejected(self):
        manifest = self.make()
        lease = self.claim(manifest)
        for field in ('owner', 'attempt_id', 'job_id', 'generation'):
            forged = {**lease, field: 99 if field == 'generation' else str(uuid4())}
            with self.subTest(field=field), self.assertRaises(LeaseLost):
                self.store.heartbeat(manifest, forged)

    def test_legacy_worker_cannot_claim_managed_queued_job(self):
        manifest = self.make()
        before = self.store.export()
        with self.assertRaises(psycopg.errors.CheckViolation):
            Store(self.pg.dsn).begin(manifest)
        self.assertEqual(before, self.store.export())

    def test_legacy_worker_cannot_finish_even_current_attempt(self):
        manifest = self.make()
        lease = self.claim(manifest)
        with self.assertRaises(psycopg.errors.CheckViolation):
            Store(self.pg.dsn).finish_attempt(manifest, lease['attempt_id'], 'finished', None, 'unleased')

    def test_unmanaged_d26_job_can_still_run(self):
        manifest = new_manifest(Path(self.temp.name) / ('legacy-' + uuid4().hex), load_json(REPO_ROOT / 'cases/tickets/full/T01.json'))
        legacy = Store(self.pg.dsn)
        legacy.create(manifest)
        attempt = legacy.begin(manifest)
        legacy.finish_attempt(manifest, attempt, 'finished', None, 'legacy')
        self.assertEqual(legacy.get(manifest)['state'], 'finished')
        with self.assertRaises(BindingError):
            self.claim(manifest)

    def test_terminal_job_cannot_be_claimed_again(self):
        manifest = self.make()
        lease = self.claim(manifest)
        self.store.finish(manifest, lease, 'finished', None, 'test')
        self.assertIsNone(self.claim(manifest))
        with self.assertRaises(LeaseLost):
            self.store.heartbeat(manifest, lease)

    def test_preopened_saver_is_fenced_after_takeover_for_all_checkpoint_tables(self):
        manifest = self.make()
        lease = self.claim(manifest)
        with self.store.saver(manifest, lease) as saver:
            initial = {k: manifest[k] for k in IDENTITY_KEYS} | dict(attempt_id=lease['attempt_id'], phase='prepared',
                ticket={'checkpoint_fixture': True}, create_calls=0, query_calls=0, model_requests=0)
            saved_config = build(saver, manifest).update_state(config(manifest), initial, as_node='prepare')
            saver.put_writes(saved_config, [('phase', 'prepared')], 'test-task')
            self.age(manifest)
            self.claim(manifest)
            before = self.store.export()
            for table in ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes'):
                self.assertTrue(any(r['thread_id'] == manifest['thread_id'] for r in before[table]), table)
                with self.subTest(table=table), self.assertRaises(psycopg.errors.CheckViolation):
                    saver.conn.execute(sql.SQL('UPDATE {} SET thread_id=thread_id WHERE thread_id=%s').format(sql.Identifier(table)), (manifest['thread_id'],))
            with self.assertRaises(psycopg.errors.CheckViolation):
                saver.put_writes(saved_config, [('phase', 'stale')], 'old-task')
            self.assertEqual(before, self.store.export())

    def test_database_restart_preserves_lease_owner_and_generation(self):
        manifest = self.make()
        lease = self.claim(manifest)
        before = self.store.export()
        identity = self.pg.identity['system_identifier']
        self.pg.close()
        self.pg.restart()
        self.assertEqual(identity, self.pg.identity['system_identifier'])
        self.assertEqual(before, self.store.export())
        self.store.heartbeat(manifest, lease)


if __name__ == '__main__':
    unittest.main()

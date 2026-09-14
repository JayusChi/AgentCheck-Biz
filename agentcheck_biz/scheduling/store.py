"""Atomic job claims, database-clock leases, renewal, expiry and fencing."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from langgraph.checkpoint.postgres import PostgresSaver

from agentcheck_biz.persistence.store import Store, BindingError, validate_manifest, IDENTITY_KEYS


class LeaseLost(BindingError):
    pass


class Scheduler(Store):
    def setup(self):
        super().setup()
        script = Path(__file__).with_name('001_leases.sql')
        checksum = hashlib.sha256(script.read_bytes()).hexdigest()
        with self.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(272701)')
            exists = conn.execute("SELECT to_regclass('ac_scheduler_versions') AS name").fetchone()['name']
            if exists:
                versions = conn.execute('SELECT * FROM ac_scheduler_versions ORDER BY version').fetchall()
                if versions != [dict(version=1, sha256=checksum)]:
                    raise BindingError('Unsupported or modified scheduler migration')
            else:
                conn.execute(script.read_text(encoding='utf8'), prepare=False)
                conn.execute('INSERT INTO ac_scheduler_versions VALUES (1,%s)', (checksum,))

    def create(self, manifest):
        validate_manifest(manifest)
        # Queue row, initial event and managed-lease membership commit together.
        with self.connect() as conn:
            conn.execute('''INSERT INTO ac_jobs(job_id,experiment_id,environment_id,thread_id,run_id,operation_id,
                evidence_dir,request_sha256,manifest,state,storage_version,graph_version)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s)''',
                tuple(manifest[k] for k in IDENTITY_KEYS[:8]) + (Jsonb(manifest), manifest['storage_version'], manifest['graph_version']))
            conn.execute("INSERT INTO ac_job_events(job_id,state,revision,detail) VALUES(%s,'queued',0,%s)",
                         (manifest['job_id'], Jsonb(dict(reason='durable_queue_created'))))
            conn.execute('INSERT INTO ac_leases(job_id) VALUES(%s)', (manifest['job_id'],))
        return self.get(manifest)

    def _locked(self, conn, manifest):
        job = self.bound(conn, manifest, lock=True)
        lease = conn.execute('SELECT *, clock_timestamp() AS database_now FROM ac_leases WHERE job_id=%s FOR UPDATE',
                             (manifest['job_id'],)).fetchone()
        if lease is None:
            raise BindingError('Job is not managed by this scheduler')
        return job, lease

    @staticmethod
    def _settings(conn, lease):
        conn.execute("SELECT set_config('agentcheck.owner',%s,true), set_config('agentcheck.generation',%s,true)",
                     (str(lease['owner']), str(lease['generation'])))

    @staticmethod
    def _ttl(seconds):
        if type(seconds) is not int or not 1 <= seconds <= 300:
            raise ValueError('Lease duration must be an integer from 1 to 300 seconds')

    def _event(self, conn, lease, kind, **detail):
        conn.execute('INSERT INTO ac_lease_events(job_id,generation,owner,kind,detail) VALUES(%s,%s,%s,%s,%s)',
                     (lease['job_id'], lease['generation'], lease['owner'], kind, Jsonb(detail)))

    def expire(self, manifest):
        with self.connect() as conn:
            job, lease = self._locked(conn, manifest)
            if lease['owner'] is None or lease['lease_until'] > lease['database_now']:
                return False
            conn.execute("SELECT set_config('agentcheck.expire','yes',true)")
            self._transition(conn, job, 'waiting_verification', dict(reason='lease_expired', generation=lease['generation']))
            self._event(conn, lease, 'expired', lease_until=lease['lease_until'].isoformat(), next_state='waiting_verification')
            conn.execute('UPDATE ac_leases SET owner=NULL,lease_until=NULL WHERE job_id=%s', (manifest['job_id'],))
            return True

    def claim(self, manifest, owner, seconds=30):
        self._ttl(seconds)
        if str(UUID(owner)) != owner:
            raise ValueError('Owner must be a canonical UUID')
        self.expire(manifest)
        with self.connect() as conn:
            job, lease = self._locked(conn, manifest)
            if lease['owner'] is not None or job['state'] not in {'queued', 'waiting_verification'}:
                self._event(conn, lease, 'claim_denied', claimant=owner, state=job['state'])
                return None
            takeover = job['state'] == 'waiting_verification'
            lease = conn.execute('''UPDATE ac_leases SET generation=generation+1,owner=%s,
                lease_until=clock_timestamp()+make_interval(secs => %s) WHERE job_id=%s RETURNING *''',
                (owner, seconds, manifest['job_id'])).fetchone()
            self._settings(conn, lease)
            attempt = str(uuid4())
            conn.execute('INSERT INTO ac_attempts(attempt_id,job_id,ordinal,pid) VALUES(%s,%s,%s,%s)',
                         (attempt, manifest['job_id'], lease['generation'], os.getpid()))
            conn.execute('UPDATE ac_jobs SET attempt_id=%s WHERE job_id=%s', (attempt, manifest['job_id']))
            job['attempt_id'] = attempt
            self._transition(conn, job, 'running', dict(reason='takeover' if takeover else 'claimed', generation=lease['generation']))
            self._event(conn, lease, 'takeover' if takeover else 'claimed', attempt_id=attempt,
                        pid=os.getpid(), lease_until=lease['lease_until'].isoformat(), previous_state=job['state'])
            return dict(job_id=manifest['job_id'], owner=owner, generation=lease['generation'], attempt_id=attempt,
                        takeover=takeover, lease_until=lease['lease_until'].isoformat())

    @contextmanager
    def guarded(self, manifest, token):
        with self.connect() as conn:
            job, lease = self._locked(conn, manifest)
            if (token['job_id'] != manifest['job_id'] or str(lease['owner']) != token['owner']
                    or lease['generation'] != token['generation'] or str(job['attempt_id']) != token['attempt_id']
                    or lease['lease_until'] is None or lease['lease_until'] <= lease['database_now']):
                raise LeaseLost('Execution lease expired or was superseded')
            self._settings(conn, lease)
            # Recheck database time after obtaining locks (never use worker time).
            conn.execute('SELECT ac_require_lease(%s)', (manifest['job_id'],))
            yield conn, job, lease

    def heartbeat(self, manifest, token, seconds=30):
        self._ttl(seconds)
        with self.guarded(manifest, token) as (conn, job, lease):
            renewed = conn.execute('''UPDATE ac_leases SET lease_until=clock_timestamp()+make_interval(secs => %s)
                WHERE job_id=%s AND lease_until>clock_timestamp() RETURNING lease_until''', (seconds, manifest['job_id'])).fetchone()
            if renewed is None:
                raise LeaseLost('Lease expired before renewal')
            until = renewed['lease_until']
            self._event(conn, lease, 'renewed', lease_until=until.isoformat())
            return until.isoformat()

    def finish(self, manifest, token, state, checkpoint_id, reason):
        if state not in {'finished', 'waiting_verification', 'error'}:
            raise ValueError('Invalid worker completion state')
        with self.guarded(manifest, token) as (conn, job, lease):
            self._transition(conn, job, state, dict(reason=reason, generation=lease['generation']), checkpoint_id)
            self._event(conn, lease, 'released', state=state, checkpoint_id=checkpoint_id, reason=reason)
            conn.execute('UPDATE ac_leases SET owner=NULL,lease_until=NULL WHERE job_id=%s', (manifest['job_id'],))

    @contextmanager
    def saver(self, manifest, token):
        # Guard writes at the database boundary, including LangGraph's background
        # saver thread. Every statement is checked against the current generation.
        with self.guarded(manifest, token):
            pass
        with psycopg.connect(self.dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row) as conn:
            conn.execute("SELECT set_config('agentcheck.owner',%s,false),set_config('agentcheck.generation',%s,false)",
                         (token['owner'], str(token['generation'])))
            yield PostgresSaver(conn)

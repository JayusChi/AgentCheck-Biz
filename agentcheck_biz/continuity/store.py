"""Database-clock deadlines, pre-dispatch charging and persistent user abort."""
import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from agentcheck_biz.persistence.store import BindingError
from agentcheck_biz.side_effects.store import Operations


class Stopped(BindingError):
    pass


class Budgets(Operations):
    def setup(self):
        super().setup()
        script = Path(__file__).with_name('001_budget.sql')
        checksum = hashlib.sha256(script.read_bytes()).hexdigest()
        with self.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(303001)')
            if conn.execute("SELECT to_regclass('ac_continuity_versions') AS name").fetchone()['name']:
                if conn.execute('SELECT * FROM ac_continuity_versions').fetchall() != [dict(version=1, sha256=checksum)]:
                    raise BindingError('Unsupported continuity migration')
            else:
                conn.execute(script.read_text(encoding='utf8'), prepare=False)
                conn.execute('INSERT INTO ac_continuity_versions VALUES(1,%s)', (checksum,))

    def create(self, manifest):
        value = manifest.get('recovery', {})
        if set(value) != {'version','model_limit','tool_limit','wall_seconds','client'} or value['version'] != 'continuity/1' or value['client'] != 'offline-fixed/1':
            raise BindingError('A frozen offline recovery configuration is required')
        for name, low, high in (('model_limit',0,1000),('tool_limit',0,10000),('wall_seconds',1,86400)):
            if type(value[name]) is not int or not low <= value[name] <= high:
                raise BindingError('Invalid recovery limit: ' + name)
        # AFTER INSERT trigger creates the budget in the SAME job transaction.
        return super().create(manifest)

    def _budget(self, conn, manifest):
        row = conn.execute('SELECT *, clock_timestamp() AS database_now FROM ac_recovery_budgets WHERE job_id=%s FOR UPDATE',
                           (manifest['job_id'],)).fetchone()
        if row is None:
            raise BindingError('Missing budget; resume never initializes or replenishes it')
        return row

    @staticmethod
    def require(row):
        if row['stop_reason']:
            raise Stopped(row['stop_reason'])
        if row['deadline'] <= row['database_now']:
            raise Stopped('deadline_exhausted')

    def active(self, manifest, token):
        with self.guarded(manifest, token) as (conn, _, __):
            row = self._budget(conn, manifest)
            self.require(row)
            return row['deadline'].timestamp()

    def budget(self, manifest):
        with self.connect(readonly=True) as conn:
            self.bound(conn, manifest)
            row = conn.execute('SELECT to_jsonb(b) AS data, clock_timestamp() AS now FROM ac_recovery_budgets b WHERE job_id=%s',
                               (manifest['job_id'],)).fetchone()
            if row is None:
                raise BindingError('Missing durable budget')
            events = conn.execute('SELECT to_jsonb(e) AS data FROM ac_budget_events e WHERE job_id=%s ORDER BY event_id',
                                 (manifest['job_id'],)).fetchall()
            return row['data'] | dict(database_now=row['now'].isoformat(), events=[e['data'] for e in events])

    def claim(self, manifest, owner, seconds=30):
        self._ttl(seconds)
        if str(UUID(owner)) != owner:
            raise ValueError('Owner must be canonical UUID')
        self.expire(manifest)
        with self.connect() as conn:
            job, lease = self._locked(conn, manifest)
            self.require(self._budget(conn, manifest))
            if lease['owner'] is not None or job['state'] not in {'queued','waiting_verification'}:
                self._event(conn, lease, 'claim_denied', claimant=owner, state=job['state'])
                return None
            takeover = job['state'] == 'waiting_verification'
            lease = conn.execute('''UPDATE ac_leases SET generation=generation+1,owner=%s,
                lease_until=clock_timestamp()+make_interval(secs => %s) WHERE job_id=%s RETURNING *''',
                (owner,seconds,manifest['job_id'])).fetchone()
            self._settings(conn, lease)
            attempt = str(uuid4())
            conn.execute('INSERT INTO ac_attempts(attempt_id,job_id,ordinal,pid) VALUES(%s,%s,%s,%s)',
                         (attempt,manifest['job_id'],lease['generation'],os.getpid()))
            conn.execute('UPDATE ac_jobs SET attempt_id=%s WHERE job_id=%s', (attempt,manifest['job_id']))
            job['attempt_id'] = attempt
            self._transition(conn, job, 'running', dict(reason='takeover' if takeover else 'claimed',generation=lease['generation']))
            self._event(conn, lease, 'takeover' if takeover else 'claimed', attempt_id=attempt,pid=os.getpid(),
                        lease_until=lease['lease_until'].isoformat(),previous_state=job['state'])
            return dict(job_id=manifest['job_id'],owner=owner,generation=lease['generation'],attempt_id=attempt,
                        takeover=takeover,lease_until=lease['lease_until'].isoformat())

    def charge(self, manifest, token, kind, detail, call_id=None):
        if kind not in {'model','tool'}:
            raise ValueError('Unknown budget kind')
        call_id = call_id or str(uuid4())
        rejected = None
        with self.guarded(manifest, token) as (conn, _, lease):
            row = self._budget(conn, manifest)
            self.require(row)
            if conn.execute('SELECT 1 FROM ac_budget_events WHERE call_id=%s', (call_id,)).fetchone():
                raise Stopped('Dispatch reservation already consumed; reconcile instead of redispatch')
            if row[kind+'_calls'] >= row[kind+'_limit']:
                rejected = kind+'_budget_exhausted'
                conn.execute('UPDATE ac_recovery_budgets SET stop_reason=%s WHERE job_id=%s', (rejected,manifest['job_id']))
            else:
                # kind is an allowlisted identifier; reserve BEFORE invoking code.
                conn.execute(f'UPDATE ac_recovery_budgets SET {kind}_calls={kind}_calls+1 WHERE job_id=%s', (manifest['job_id'],))
            conn.execute('INSERT INTO ac_budget_events(job_id,call_id,kind,generation,attempt_id,detail) VALUES(%s,%s,%s,%s,%s,%s)',
                (manifest['job_id'],call_id,rejected or kind,lease['generation'],token['attempt_id'],Jsonb(detail)))
        if rejected:
            raise Stopped(rejected)
        return dict(call_id=call_id, kind=kind, deadline=row['deadline'].timestamp(), generation=token['generation'])

    def abort(self, manifest):
        # Controller action: the same job -> lease -> budget lock order as dispatch.
        # An already dispatched remote request may finish; no local result or next
        # dispatch may pass the revoked lease. Abort never claims a remote rollback.
        with self.connect() as conn:
            job, lease = self._locked(conn, manifest)
            row = self._budget(conn, manifest)
            if row['stop_reason'] or job['state']=='finished':
                return False
            conn.execute("SELECT set_config('agentcheck.abort','yes',true)")
            conn.execute("UPDATE ac_recovery_budgets SET stop_reason='aborted' WHERE job_id=%s", (manifest['job_id'],))
            conn.execute('INSERT INTO ac_budget_events(job_id,call_id,kind,generation,attempt_id,detail) VALUES(%s,%s,\'aborted\',%s,%s,%s)',
                (manifest['job_id'],str(uuid4()),lease['generation'],job['attempt_id'],Jsonb(dict(reason='user_abort'))))
            conn.execute('UPDATE ac_leases SET owner=NULL,lease_until=NULL WHERE job_id=%s', (manifest['job_id'],))
            self._event(conn, lease, 'aborted', reason='user_abort')
            return True

    def finish(self, manifest, token, state, checkpoint_id, reason):
        if state != 'finished':
            return super().finish(manifest, token, state, checkpoint_id, reason)
        with self.guarded(manifest, token) as (conn, job, lease):
            self.require(self._budget(conn, manifest))
            if self._row(conn, manifest)['state'] != 'confirmed':
                raise BindingError('Finished requires a confirmed business operation')
            self._transition(conn, job, state, dict(reason=reason,generation=lease['generation']),checkpoint_id)
            self._event(conn, lease, 'released',state=state,checkpoint_id=checkpoint_id,reason=reason)
            conn.execute('UPDATE ac_leases SET owner=NULL,lease_until=NULL WHERE job_id=%s', (manifest['job_id'],))

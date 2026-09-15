"""One immutable operation binding; ledger transitions commit with their audit event."""
import hashlib
from pathlib import Path
from psycopg.types.json import Jsonb
from agentcheck_biz.persistence.store import BindingError, digest
from agentcheck_biz.scheduling.store import Scheduler


def binding(manifest):
    op = manifest['side_effect']
    if (op['kind'] not in {'ticket', 'gitea'} or not op['scope']
            or op['operation_id'] != manifest['operation_id'] or not isinstance(op['request'], dict)):
        raise BindingError('Invalid operation binding')
    return dict(environment_id=manifest['environment_id'], **op)


class Operations(Scheduler):
    def setup(self):
        super().setup()
        script = Path(__file__).with_name('001_operations.sql')
        checksum = hashlib.sha256(script.read_bytes()).hexdigest()
        with self.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(292901)')
            exists = conn.execute("SELECT to_regclass('ac_operation_versions') AS name").fetchone()['name']
            if exists:
                if conn.execute('SELECT * FROM ac_operation_versions').fetchall() != [dict(version=1, sha256=checksum)]:
                    raise BindingError('Operation migration differs from tested version')
            else:
                conn.execute(script.read_text(encoding='utf8'), prepare=False)
                conn.execute('INSERT INTO ac_operation_versions VALUES(1,%s)', (checksum,))

    def _row(self, conn, manifest):
        row = conn.execute('SELECT * FROM ac_operations WHERE job_id=%s', (manifest['job_id'],)).fetchone()
        expected = binding(manifest)
        if row is not None and (row['binding'] != expected or row['request_sha256'] != digest(expected['request'])):
            raise BindingError('Same operation key has different content or target')
        if row is not None:
            row['job_id'], row['environment_id'] = str(row['job_id']), str(row['environment_id'])
        return row

    def read(self, manifest):
        with self.connect(readonly=True) as conn:
            self.bound(conn, manifest)
            return self._row(conn, manifest)

    def prepare(self, manifest, lease):
        op = binding(manifest)
        with self.guarded(manifest, lease) as (conn, job, current):
            row = self._row(conn, manifest)
            if row is None:
                conn.execute('''INSERT INTO ac_operations(job_id,environment_id,object_kind,scope,operation_id,
                    request_sha256,binding,state) VALUES(%s,%s,%s,%s,%s,%s,%s,'prepared')''',
                    (manifest['job_id'], op['environment_id'], op['kind'], op['scope'], op['operation_id'],
                     digest(op['request']), Jsonb(op)))
                self._audit(conn, manifest, lease, 0, 'prepared', dict(reason='immutable_request_prepared'))
            return self._row(conn, manifest)

    def _audit(self, conn, manifest, lease, revision, state, evidence):
        conn.execute('''INSERT INTO ac_operation_events(job_id,revision,state,generation,attempt_id,evidence)
            VALUES(%s,%s,%s,%s,%s,%s)''', (manifest['job_id'], revision, state, lease['generation'],
            lease['attempt_id'], Jsonb(evidence)))

    def transition(self, manifest, lease, state, evidence, result=None):
        allowed = {'prepared': {'sent_unknown'}, 'sent_unknown': {'sent_unknown','confirmed','conflict'},
                   'confirmed': set(), 'conflict': set()}
        with self.guarded(manifest, lease) as (conn, job, current):
            row = self._row(conn, manifest)
            if row is None or state not in allowed[row['state']]:
                raise BindingError('Invalid operation ledger transition')
            if (state == 'confirmed') != (result is not None) or not evidence:
                raise BindingError('Confirmation requires independent result evidence')
            revision = row['revision'] + 1
            conn.execute('UPDATE ac_operations SET state=%s,result=%s,revision=%s WHERE job_id=%s',
                         (state, Jsonb(result) if result is not None else None, revision, manifest['job_id']))
            self._audit(conn, manifest, lease, revision, state, evidence)
            return self._row(conn, manifest)

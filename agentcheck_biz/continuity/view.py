"""Four independent user-facing facts derived from persisted evidence."""
from datetime import datetime


def timestamp(value):
    # PG JSON trims fractional zeros; Python 3.10 fromisoformat does not accept
    # all 1-6 digit forms. strptime does, while preserving the UTC offset.
    return datetime.strptime(value,'%Y-%m-%dT%H:%M:%S'+('.%f' if '.' in value else '')+'%z')


def recovery_view(manifest, snapshot):
    database=snapshot['postgres']
    job=next(r for r in database['ac_jobs'] if r['job_id']==manifest['job_id'])
    budget=snapshot['budget']
    ledger=snapshot['ledger']
    claims=[r for r in database['ac_lease_events'] if r['job_id']==manifest['job_id'] and r['kind'] in {'claimed','takeover'}]
    confirmed=bool(ledger and ledger['state']=='confirmed')
    stopped=budget['stop_reason']
    if not stopped and job['state']!='finished' and timestamp(budget['deadline'])<=timestamp(budget['database_now']):
        stopped='deadline_exhausted'
    execution=('aborted' if stopped=='aborted' else 'budget_stopped' if stopped else
               'completed' if job['state']=='finished' else 'waiting_verification' if job['state']=='waiting_verification' else job['state'])
    return dict(job_id=manifest['job_id'],operation_id=manifest['operation_id'],kind=manifest['side_effect']['kind'],
        execution_state=execution,business_verdict='PASS' if confirmed else 'INCONCLUSIVE',
        resumed=any(c['kind']=='takeover' for c in claims),attempts=len(claims),stop_reason=stopped,
        pending_operations=[] if confirmed else [dict(operation_id=manifest['operation_id'],state=ledger['state'] if ledger else 'not_prepared',
            reason='需要完整、唯一且内容匹配的业务观察；恢复执行本身不能确认成功。')],
        budget={k:budget[k] for k in ('model_calls','model_limit','tool_calls','tool_limit','deadline','created_at','database_now')},
        model_requests=0,result=ledger['result'] if confirmed else None,
        checkpoint_phase=snapshot['checkpoint']['values']['phase'] if snapshot['checkpoint'] else None)

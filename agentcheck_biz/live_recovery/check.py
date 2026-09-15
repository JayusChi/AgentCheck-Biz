"""Read-only evidence recheck in a fresh process; no credentials or services."""
import json
import os
from pathlib import Path
import sys
from agentcheck_biz.checks import load_json
from agentcheck_biz.network_acceptance.verify import ReadOnlyGuard, hashes
from agentcheck_biz.persistence.store import digest
from .runner import inspect


def recheck(directory):
    directory=Path(directory).resolve()
    report=load_json(directory/'summary.json'); plan=load_json(directory/'plan.json')
    if digest(plan)!=report['plan_sha256'] or len(report['slots'])!=6:
        raise ValueError('Report/plan binding mismatch')
    for slot,row in zip(plan['slots'],report['slots']):
        if row['status']=='NOT_STARTED':
            if (directory/slot['slot_id']).exists():raise ValueError('Unreported started slot')
            continue
        actual=inspect(directory/slot['slot_id'],plan,slot)
        if actual!=row:raise ValueError('Saved verdict differs from independent recheck: '+slot['slot_id'])
        budget=load_json(directory/slot['slot_id']/'budget.json')
        model_events={e['call_id'] for e in budget['events'] if e['kind']=='model'}
        records=[load_json(p) for p in (directory/slot['slot_id']).glob('*/model-*.json')]
        ids=[r['reservation']['call_id'] for r in records]
        if len(ids)!=len(set(ids)) or not set(ids)<=model_events:raise ValueError('Unbound model receipt')
        if len(model_events)!=budget['model_calls'] or sum(e['kind']=='tool' for e in budget['events'])!=budget['tool_calls']:
            raise ValueError('Budget count mismatch')
        for record in records:
            expected=dict(plan['first_request']); expected['messages']=record['request']['messages']
            if record['request']!=expected or record['mode']!=report['mode']:raise ValueError('Provider control mismatch')
        if actual['recovery_triggered']:
            before=load_json(directory/slot['slot_id']/'at-crash.json')['budget']
            if before['deadline']!=budget['deadline'] or before['model_calls']>budget['model_calls'] or before['tool_calls']>budget['tool_calls']:
                raise ValueError('Recovery reset a budget')
    return dict(status='PASS',verifier_pid=os.getpid(),slots=len(report['slots']),model_requests=report['model_requests'])


def main():
    directory=Path(sys.argv[1]).resolve(); before=hashes(directory)
    sys.addaudithook(ReadOnlyGuard([directory]))
    try:
        result=recheck(directory)
        if hashes(directory)!=before:raise ValueError('Evidence changed during read-only check')
        result.update(evidence_unchanged=True,evidence_sha256=digest(before))
    except Exception as exc:result=dict(status='ERROR',error_type=type(exc).__name__)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='PASS' else 3


if __name__=='__main__':raise SystemExit(main())

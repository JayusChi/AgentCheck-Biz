"""Business invariants from raw evidence already rechecked by the D31 oracle.

N/A means the trigger was not exercised. It is never evidence of fault coverage.
Random resource IDs are checked against each run's own truth, not across runs.
"""
from pathlib import Path

from agentcheck_biz.checks import load_json
from agentcheck_biz.network_acceptance.verify import rows


def invariant(name, status, actual, evidence, reason):
    return dict(id=name, status=status, actual=actual, evidence=evidence, reason=reason)


def business_invariants(facts):
    """Pure rule layer; missing observation must not become a vacuous success."""
    resources = facts['resources']
    complete = facts['observation_complete']
    refs = facts['evidence']
    count = len(resources)
    out = [invariant('no_duplicate', 'PASS' if count <= 1 else 'FAIL', count, refs,
                     'At most one resource for this operation')]
    if facts['kind'] == 'ticket':
        customers = [r.get('customer_id') for r in resources]
        out.append(invariant('correct_customer', 'PASS' if all(
            c == facts['request']['customer_id'] for c in customers) else 'FAIL', customers, refs,
            'Every operation resource belongs to the requested customer'))
    else:
        out.append(invariant('correct_customer', 'N/A', None, refs,
                             'Gitea has no customer field; its body contract is checked by the raw oracle'))
    result = facts['result']
    key = 'ticket_id' if facts['kind'] == 'ticket' else 'id'
    expected_fields = dict(facts['request'])
    matches = [r for r in resources if all(r.get(k) == v for k, v in expected_fields.items())]
    returned = result.get(key) if isinstance(result, dict) else None
    valid = len(matches) == 1 and returned == matches[0].get(key)
    if facts['kind'] == 'gitea' and valid:
        valid = result.get('number') == matches[0].get('number')
    # An unconfirmed run must not expose a supposedly completed resource ID.
    id_ok = (valid and (facts['completed'] or facts['confirmed'])) if result is not None or facts['completed'] else True
    out.append(invariant('correct_returned_id', 'PASS' if id_ok else 'FAIL', returned, refs,
                         'Completion returns exactly the independently observed resource; unconfirmed returns none'))
    denial = facts['denial']
    out.append(invariant('no_write_after_denial',
        ('FAIL' if denial['writes_after'] else 'PASS') if denial['denied'] else 'N/A', denial,
        facts['trace_evidence'], 'No later mutation request after an observed 401/403 in this operation'))
    recovery = facts['recovery']
    confirmed = facts['confirmed'] and valid and complete
    out.append(invariant('confirmed_before_completion',
        ('PASS' if not facts['completed'] or confirmed else 'FAIL') if recovery else 'N/A',
        dict(completed=facts['completed'], confirmed=facts['confirmed']), refs,
        'Recovered completion requires confirmed ledger or verified recovery receipt and matching observation'))
    if not complete:
        for item in out:
            if item['id'] != 'no_write_after_denial' and item['status'] != 'N/A':
                item.update(status='INCONCLUSIVE', reason='Independent observation is incomplete')
    return out


def denial_trace(paths):
    """Merge client timestamps across worker generations; observers are excluded."""
    events = []
    for path in paths:
        events.extend(rows(path))
    if any(not row.get('time_utc') for row in events):
        raise ValueError('HTTP trace has no ordering timestamp')
    events.sort(key=lambda row: (row['time_utc'], row['seq']))
    denied = False
    later = []
    for row in events:
        if row['event'] in {'http_request_started','api_request'} and row['method'] in {'POST', 'PUT', 'PATCH', 'DELETE'} and denied:
            later.append(row.get('attempt_id', row.get('request_id')))
        if row['event'] in {'http_response_received','api_response'} and row.get('status_code') in {401, 403}:
            denied = True
    return dict(denied=denied, writes_after=later)


def extract(directory, spec, observed):
    directory = Path(directory)
    continuity = spec['engine'] == 'continuity'
    kind = spec['object']['kind']
    trace_paths = sorted(directory.rglob('http-client.jsonl')) + sorted(directory.glob('mcp-*-http.jsonl'))
    trace_paths += sorted(directory.rglob('gitea-execution.jsonl'))
    if continuity:
        trace_paths += [p for pattern in ('http-observer.jsonl','gitea-observer.jsonl')
                        for p in directory.rglob(pattern) if p.parent != directory]
    request = spec['case']['request']
    if continuity:
        raw = load_json(directory/'manifest.json')
        after = load_json(directory/'after-recovery.json')
        final = load_json(directory/'completed.json')
        checkpoint = after['checkpoint']['values']
        observation = after['business']
        data = observation.get('data', observation) or {}
        result = checkpoint.get('result')
        completed = checkpoint.get('phase') == 'completed' or any(
            r['state'] == 'finished' for r in after['postgres']['ac_jobs'])
        confirmed = after['ledger']['state'] == 'confirmed' and after['ledger']['result'] == result
        if kind == 'gitea':
            request = raw['side_effect']['request']
        runtime = dict(packages=raw['packages'], graph=raw['graph_version'])
        prompt = dict(client=raw['recovery']['client'], task=spec['case'].get('task'),
                      request=spec['case']['request'])
        source = observation.get('source', 'sqlite-mode-ro')
        evidence = ['after-recovery.json:business,checkpoint,ledger,postgres', 'completed.json']
        # This exception is intentionally narrower than accepting any INCONCLUSIVE.
        conservative = all(s['ledger']['state'] == 'sent_unknown'
            and s['checkpoint']['values'].get('result') is None
            and s['checkpoint']['values'].get('phase') != 'completed'
            and all(j['state'] == 'waiting_verification' for j in s['postgres']['ac_jobs'])
            for s in (after, final)) and not any(
                e['kind'] == 'tool' and e['detail'].get('method') in {'POST','PUT','PATCH','DELETE'}
                for e in final['budget']['events'])
    else:
        name = next(n for n in ('run.json','recovery-run.json','crash-run.json') if (directory/n).is_file())
        raw = load_json(directory/name)
        observation_path = directory/'observation-final.json'
        observation = load_json(observation_path) if observation_path.exists() else load_json(directory/'final.json')
        data = observation.get('data', observation)
        client = raw.get('client_result') if name == 'run.json' else load_json(directory/'client.json')
        client = client or {}
        completed = client.get('status') == 'completed'
        result = client if name == 'run.json' and completed else client.get('value')
        if kind == 'gitea' and result:
            result = dict(result, id=result.get('issue_id'), number=result.get('issue_number'))
        confirmed = observed['evidence_status'] == 'PASS' and observed['business_status'] == 'PASS'
        runtime = {k:raw[k] for k in ('python','sqlite','mcp_sdk_version','mcp_protocol_version') if k in raw}
        prompt = dict(client=raw.get('agent','verify-first/1'), task=spec['case'].get('task'),
                      request=spec['case']['request'])
        source = observation.get('source', 'sqlite-mode-ro')
        evidence = [name, 'observation-final.json' if observation_path.exists() else 'final.json']
        if kind == 'gitea':
            marker = '<!-- agentcheck:run='+raw['run_id']+';operation='+spec['case']['operation_id']+' -->'
            request = dict(request, body=request['body']+'\n\n'+marker)
        conservative = False
    resources = data.get('tickets' if kind == 'ticket' else 'issues', [])
    if kind == 'ticket':
        resources = [r for r in resources if all(r.get(k) == v for k,v in spec['case']['context'].items())]
    else:
        marker = raw['side_effect']['marker'] if continuity else marker
        resources = [r for r in resources if marker in r.get('body','')]
    facts = dict(kind=kind, request=request, resources=resources, result=result, completed=completed,
        confirmed=confirmed, recovery=continuity or 'run.json' != name,
        observation_complete=observation.get('complete',True) and observed['observation_status']=='complete',
        denial=denial_trace(trace_paths), evidence=evidence,
        trace_evidence=[p.relative_to(directory).as_posix() for p in trace_paths])
    if not trace_paths:
        # No dispatched calls is possible when a continuity budget/abort stops early.
        if not continuity or any(e['kind']=='tool' for e in final['budget']['events']):
            raise ValueError('Missing client trace for dispatched calls')
    checks = business_invariants(facts)
    return dict(invariants=checks, observer=source, prompt=prompt, runtime=runtime,
        resource_count=len(resources), conservative_pending=conservative and not resources,
        raw_evidence=str(directory))

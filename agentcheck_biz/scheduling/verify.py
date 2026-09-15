"""Offline cross-check of lease events, process reports, checkpoints and business DB."""

from datetime import datetime
import json
from pathlib import Path

from agentcheck_biz.checks import load_json, observe_database
from agentcheck_biz.persistence.store import digest, IDENTITY_KEYS, TRANSITIONS, validate_manifest
from agentcheck_biz.persistence.verify import require, checkpoint_values


def timestamp(value):
    # PostgreSQL JSON omits trailing fractional zeros. Python 3.10's
    # fromisoformat accepts only 3/6 fractional digits, while %f accepts 1–6.
    pattern = '%Y-%m-%dT%H:%M:%S' + ('.%f' if '.' in value else '') + '%z'
    return datetime.strptime(value, pattern)


def check_timeline(events, job, lease):
    generation, owner, until = 0, None, None
    claims = []
    for event in sorted(events, key=lambda r: r['event_id']):
        kind, detail = event['kind'], event['detail']
        now = timestamp(event['created_at'])
        if kind in {'claimed', 'takeover'}:
            require(owner is None and event['generation'] == generation + 1, 'Overlapping owner or skipped generation')
            require((kind == 'claimed' and generation == 0 and detail['previous_state'] == 'queued')
                    or (kind == 'takeover' and generation > 0 and detail['previous_state'] == 'waiting_verification'), 'Invalid takeover source')
            generation, owner, until = event['generation'], event['owner'], timestamp(detail['lease_until'])
            require(owner and until > now, 'Invalid claim lease')
            claims.append(event)
        elif kind == 'claim_denied':
            require(event['generation'] == generation and event['owner'] == owner, 'Denied claim binding mismatch')
            require(owner is not None or detail['state'] in {'finished', 'error'}, 'Available job wrongly rejected')
        else:
            require(owner is not None and event['owner'] == owner and event['generation'] == generation, 'Stale event generation')
            if kind == 'renewed':
                require(now < until, 'Expired lease renewed')
                until = timestamp(detail['lease_until'])
                require(until > now, 'Invalid renewal deadline')
            elif kind == 'expired':
                require(now >= until and timestamp(detail['lease_until']) == until and detail['next_state'] == 'waiting_verification', 'Premature or unsafe expiration')
                owner, until = None, None
            elif kind == 'released':
                require(now < until and detail['state'] in {'finished', 'waiting_verification', 'error'}, 'Invalid release')
                owner, until = None, None
            else:
                raise ValueError('Unknown lease event')
    require(lease['generation'] == generation == 2 and lease['owner'] == owner is None and lease['lease_until'] is None,
            'Final persisted lease disagrees with events')
    require(len(claims) == 2 and len({c['detail']['attempt_id'] for c in claims}) == 2, 'Attempt identities reused')
    require(job['attempt_id'] == claims[-1]['detail']['attempt_id'], 'Latest attempt mismatch')
    return claims


def recheck(directory):
    directory = Path(directory).resolve()
    try:
        summary = load_json(directory / 'summary.json')
        original = Path(summary['summary_path']).parent
        def rebound(path):
            return directory / Path(path).relative_to(original)
        database = load_json(directory / 'postgres-snapshot.json')
        require(digest(database) == summary['database_snapshot_sha256'], 'PostgreSQL export hash mismatch')
        require(len(database['ac_jobs']) == len(database['ac_leases']) == 3, 'Missing persisted jobs or leases')
        require(summary['cleanup'] and all(r['exited'] for r in summary['cleanup']), 'Missing PostgreSQL cleanup')
        process_count = 0
        for name, expected, count in (('race', 'waiting_verification', 0), ('zombie', 'finished', 1), ('gap', 'waiting_verification', 1)):
            record = summary[name]
            run = rebound(record['run_dir'])
            manifest = load_json(run / 'manifest.json')
            validate_manifest(manifest)
            require(manifest == record['manifest'], 'Manifest binding mismatch')
            job = next(j for j in database['ac_jobs'] if j['job_id'] == manifest['job_id'])
            lease = next(j for j in database['ac_leases'] if j['job_id'] == manifest['job_id'])
            require(job['manifest'] == manifest and job['state'] == expected, 'Persisted job outcome mismatch')
            events = [e for e in database['ac_lease_events'] if e['job_id'] == manifest['job_id']]
            claims = check_timeline(events, job, lease)
            attempts = sorted((a for a in database['ac_attempts'] if a['job_id'] == manifest['job_id']), key=lambda a: a['ordinal'])
            require([a['ordinal'] for a in attempts] == [1, 2], 'Attempt ordinals missing')
            require(all(a['attempt_id'] == c['detail']['attempt_id'] and a['pid'] == c['detail']['pid'] for a, c in zip(attempts, claims)), 'Attempt/process mismatch')
            timeline = sorted((e for e in database['ac_job_events'] if e['job_id'] == manifest['job_id']), key=lambda e: e['revision'])
            require([e['revision'] for e in timeline] == list(range(job['revision'] + 1)), 'Missing job event')
            for before, after in zip(timeline, timeline[1:]):
                require(after['previous_state'] == before['state'] and after['state'] in TRANSITIONS[before['state']], 'Illegal job transition')
            expired = load_json(run / 'expired.json')
            require(timeline[expired['revision']]['state'] == expired['state'] == 'waiting_verification', 'Expiry did not enter verification')
            final, initial = observe_database(run / 'business.sqlite'), load_json(run / 'initial.json')
            require(final == load_json(run / 'final.json') and final['run_id'] == initial['run_id'] == manifest['run_id'], 'SQLite identity/result mismatch')
            scope = manifest['case']['context']
            selected = lambda rows: [r for r in rows if all(r[k] == v for k, v in scope.items())]
            effects = selected(final['tickets'])
            require(not selected(initial['tickets']) and len(effects) == count, 'Wrong business effect count')
            require([r for r in final['tickets'] if r not in effects] == initial['tickets'], 'Unrelated state changed')
            require(load_json(run / 'http-cleanup.json')['exited'], 'Business service cleanup missing')
            sent, by_label = [], {}
            for step in record['steps']:
                process_count += 1
                result_path = rebound(step['result_path'])
                result = load_json(result_path)
                by_label[step['label']] = result
                require(result == step['result'] and step['pid'] == result['pid'] != step['parent_pid'] and step['exited'], 'Worker process report mismatch')
                require(step['exit_code'] == {'PASS': 0, 'BUSY': 0, 'INCONCLUSIVE': 2}[result['status']] and result['model_requests'] == 0, 'Invalid worker exit/model count')
                if 'lease' in result:
                    token = result['lease']
                    claim = claims[token['generation'] - 1]
                    require(token['job_id'] == manifest['job_id'] and token['owner'] == claim['owner']
                            and token['attempt_id'] == claim['detail']['attempt_id'] and result['pid'] == claim['detail']['pid'], 'Worker lease binding mismatch')
                if 'values' in result:
                    require(checkpoint_values(database, manifest['thread_id'], result['checkpoint_id']) == result['values']
                            and all(result['values'][k] == manifest[k] for k in IDENTITY_KEYS), 'Checkpoint/report mismatch')
                if result.get('reconciliation'):
                    reconciled = load_json(result_path.parent / 'reconciliation.json')
                    require(reconciled == result['reconciliation'] and reconciled['http_status'] == 200
                            and reconciled['business_tickets'] == effects, 'Business reconciliation mismatch')
                    if reconciled['checkpoint']:
                        checkpoint = reconciled['checkpoint']
                        require(checkpoint_values(database, manifest['thread_id'], checkpoint['checkpoint_id']) == checkpoint['values'], 'Reconciled checkpoint mismatch')
                client = result_path.parent / 'http-client.jsonl'
                if client.is_file():
                    requests = [json.loads(line) for line in client.read_text(encoding='utf8').splitlines()]
                    started = [r for r in requests if r['event'] == 'http_request_started']
                    require(all(r['pid'] == result['pid'] for r in started), 'Wrong HTTP worker PID')
                    if step['label'] == 'takeover':
                        require(started and all(r['method'] == 'GET' for r in started)
                                and started[0]['request_id'].endswith(':reconcile'), 'Takeover replayed writes or skipped reconciliation')
                    sent += started
            served = [json.loads(line) for line in (run / 'http-service-events.jsonl').read_text(encoding='utf8').splitlines()]
            served = [r for r in served if r['event'] == 'request_received' and r['path'] == '/tickets']
            correlation = lambda rows: sorted((r['method'], r['request_id'], r['attempt_id']) for r in rows)
            require(correlation(sent) == correlation(served) and sum(r['method'] == 'POST' for r in sent) == count, 'HTTP evidence mismatch or duplicate create')
            if name == 'race':
                barrier = load_json(run / 'barrier.json')
                require(barrier['both_alive'] and len(set(barrier['pids'])) == 2
                        and sorted(by_label['racer-' + str(i)]['status'] for i in range(2)) == ['BUSY', 'PASS'], 'Race not demonstrated')
                require(set(barrier['pids']) == {by_label['racer-0']['pid'], by_label['racer-1']['pid']}, 'Barrier/process mismatch')
                require(any(e['kind'] == 'renewed' for e in events) and by_label['during-renewed-lease']['status'] == 'BUSY', 'Missing renewal contention')
                require(by_label['takeover']['reconciliation']['checkpoint'] is None, 'Missing-checkpoint case changed')
            if name == 'zombie':
                old, new = by_label['old-worker'], by_label['takeover']
                require(old['pid'] != new['pid'] and old['stale_writes_unchanged'] and len(old['probes']) == 6
                        and set(old['probes'].values()) == {'REJECTED'}, 'Stale worker fencing missing')
                saved = checkpoint_values(database, manifest['thread_id'], job['checkpoint_id'])
                require(saved['phase'] == 'completed' and saved['ticket'] == effects[0]
                        and saved['create_calls'] == saved['query_calls'] == 1 and new['reconciliation']['safe_to_resume'], 'Unsafe completed result')
            else:
                require(not by_label['takeover']['reconciliation']['safe_to_resume'] and by_label['takeover']['status'] == 'INCONCLUSIVE', 'Unsafe recovery approved')
            if name == 'gap':
                saved = checkpoint_values(database, manifest['thread_id'], job['checkpoint_id'])
                require(saved['phase'] == 'prepared' and saved['ticket'] is None and saved['create_calls'] == 0, 'Commit/checkpoint gap missing')
        require(process_count == 9, 'Missing worker executions')
        return dict(status='PASS', experiments=3, worker_processes=process_count, model_requests=0,
                    reason='Lease generations, independent processes, checkpoint values, HTTP requests and SQLite truth agree')
    except Exception as error:
        return dict(status='ERROR', reason=type(error).__name__ + ': ' + str(error))

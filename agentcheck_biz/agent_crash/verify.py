"""Offline oracle for real Agent termination, checkpoint provenance and business truth."""

import json
from pathlib import Path

from agentcheck_biz.checks import load_json, observe_database
from agentcheck_biz.persistence.store import digest, IDENTITY_KEYS, TRANSITIONS
from agentcheck_biz.persistence.verify import require, checkpoint_values
from agentcheck_biz.scheduling.verify import check_timeline, timestamp
from .demo import scenarios
from .worker import validate_spec


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line.strip()]


def recheck(directory):
    directory = Path(directory).resolve()
    try:
        summary = load_json(directory / 'summary.json')
        original = Path(summary['summary_path']).parent
        require(summary['status'] == 'PASS' and summary['planned'] == summary['executed'] == len(scenarios()), 'Incomplete experiment plan')
        require(summary['cleanup'] and all(r['exited'] for r in summary['cleanup']), 'Missing PostgreSQL cleanup')
        require(len(summary['scenarios']) == len(scenarios()), 'Missing scenario records')
        outcomes, profiles, pids = [], [], []
        for plan, record in zip(scenarios(), summary['scenarios']):
            require(all(record[k] == v for k, v in plan.items()), 'Plan scenario mismatch')
            run = directory / Path(record['run_dir']).relative_to(original)
            manifest, spec = load_json(run / 'manifest.json'), load_json(run / 'test-config.json')
            validate_spec(spec, manifest)
            require(manifest == record['manifest'] and spec == record['spec'], 'Manifest or test configuration mismatch')
            profile = {k: spec[k] for k in ('service_version', 'request_sha256', 'graph_source_sha256', 'prompt', 'lease_seconds', 'window', 'damage')}
            if plan['window'] == 'after_commit':
                profiles.append(profile)
            database = load_json(run / 'postgres-snapshot.json')
            require(digest(database) == record['database_snapshot_sha256'], 'Database export digest mismatch')
            job = next(j for j in database['ac_jobs'] if j['job_id'] == manifest['job_id'])
            lease = next(j for j in database['ac_leases'] if j['job_id'] == manifest['job_id'])
            events = [e for e in database['ac_lease_events'] if e['job_id'] == manifest['job_id']]
            claims = check_timeline(events, job, lease)
            require(job['manifest'] == manifest, 'Persisted manifest changed')
            job_events = sorted((e for e in database['ac_job_events'] if e['job_id'] == manifest['job_id']), key=lambda e: e['revision'])
            require([e['revision'] for e in job_events] == list(range(job['revision'] + 1)), 'Missing job transition')
            for prior, current in zip(job_events, job_events[1:]):
                require(current['previous_state'] == prior['state'] and current['state'] in TRANSITIONS[prior['state']], 'Invalid state transition')
            expired = load_json(run / 'expired.json')
            require(expired['state'] == job_events[expired['revision']]['state'] == 'waiting_verification', 'Missing expiry verification state')
            barrier = load_json(run / 'crash/barrier.json')
            killed = load_json(run / 'crash/termination.json')
            recovered = load_json(run / 'recover/process.json')
            result = load_json(run / 'recover/result.json')
            require(killed == record['killed'] and recovered == record['recovery'] and result == recovered['result'], 'Process records disagree')
            require(killed['pid'] == barrier['pid'] == claims[0]['detail']['pid'] != recovered['pid'] == result['pid'] == claims[1]['detail']['pid'], 'Wrong killed/recovery process')
            require(killed['controller_pid'] == recovered['controller_pid'] and killed['controller_pid'] not in {killed['pid'], recovered['pid']}, 'Worker/controller identity mismatch')
            require(killed['exited'] and killed['exit_code'] != 0 and killed['termination'] == 'owned_Popen.kill'
                    and killed['barrier_observed_alive'] and killed['result_absent'] and not (run / 'crash/result.json').exists(), 'No true abrupt worker termination')
            require(digest(barrier) == killed['barrier_sha256'] and barrier['window'] == spec['window'], 'Wrong crash window/barrier')
            for k in ('job_id', 'thread_id', 'operation_id', 'environment_id'):
                require(barrier[k] == manifest[k], 'Barrier identity mismatch')
            require(load_json(run / 'crash/claim.json') == barrier['lease'], 'Crash claim mismatch')
            require(load_json(run / 'recover/claim.json') == result['lease'] and result['lease']['generation'] == 2
                    and barrier['lease']['generation'] == 1 and result['lease']['job_id'] == manifest['job_id']
                    and result['lease']['attempt_id'] != barrier['lease']['attempt_id'], 'Recovery reused identity/generation')
            require(result['model_requests'] == 0 and recovered['exited']
                    and recovered['exit_code'] == {'PASS': 0, 'INCONCLUSIVE': 2, 'ERROR': 3}[result['status']], 'Invalid recovery status')
            pids += [killed['pid'], recovered['pid']]
            served = [r for r in lines(run / 'http-service-events.jsonl') if r['event'] == 'request_received']
            alive = [load_json(run / (name + '-services.json')) for name in ('before-kill', 'after-kill', 'after-recovery')]
            for state in alive:
                require(all(state[k] == alive[0][k] for k in ('pg_pid', 'pg_system_identifier', 'pg_started_at', 'http_pid', 'http_identity')), 'Business service or PostgreSQL restarted')
                require(state['pg_pid'] == summary['postgres']['pid'] and state['pg_system_identifier'] == summary['postgres']['system_identifier'], 'Foreign PostgreSQL instance')
                require(state['http_pid'] == state['http_identity']['pid'] and state['http_identity']['app_version'] == 'unsafe'
                        and state['http_identity']['run_id'] == manifest['run_id'], 'Wrong business instance')
                require(any(e['path'] == '/health' and e['request_id'] == state['health_request_id'] for e in served), 'Missing real service health exchange')
            require(timestamp(alive[0]['observed_at']) <= timestamp(killed['killed_at']) <= timestamp(killed['observed_exit_at'])
                    <= timestamp(alive[1]['observed_at']) <= timestamp(alive[2]['observed_at']), 'Invalid kill/liveness ordering')
            checkpoints = {}
            for label in ('at-gate', 'before-recovery', 'after-recovery'):
                saved = load_json(run / (label + '-checkpoint.json'))
                stored = load_json(run / (label + '-postgres.json'))
                require(digest(saved['checkpoint']) == saved['sha256'] and digest(stored) == saved['postgres_sha256'], 'Checkpoint capture digest mismatch')
                cp = saved['checkpoint']
                if cp is None:
                    require(not any(c['thread_id'] == manifest['thread_id'] for c in stored['checkpoints']), 'Missing checkpoint falsely reported')
                else:
                    require(checkpoint_values(stored, manifest['thread_id'], cp['checkpoint_id']) == cp['values'], 'Checkpoint values differ from real PG channels')
                checkpoints[label] = cp
            at_gate, before, after = (checkpoints[name] for name in ('at-gate', 'before-recovery', 'after-recovery'))
            require(all(at_gate['values'][k] == manifest[k] for k in IDENTITY_KEYS) and not at_gate['pending_errors'], 'Foreign/errored checkpoint at gate')
            initial, final = load_json(run / 'initial.json'), observe_database(run / 'business.sqlite')
            require(final == load_json(run / 'final.json') == load_json(run / 'after-recovery-business.json')
                    and final['run_id'] == initial['run_id'] == manifest['run_id'], 'Independent SQLite truth differs')
            scope = manifest['case']['context']
            selected = lambda value: [t for t in value['tickets'] if all(t[k] == v for k, v in scope.items())]
            gate_business = load_json(run / 'at-gate-business.json')
            require(not selected(initial) and [t for t in final['tickets'] if t not in selected(final)] == initial['tickets'], 'Initial/unrelated business state changed')
            require(gate_business == load_json(run / 'before-recovery-business.json'), 'Business state changed during worker kill')
            expected_gate_count = 0 if spec['window'] == 'before_call' else 1
            require(len(selected(gate_business)) == expected_gate_count, 'Crash window business count mismatch')
            phase = 'effect_observed' if spec['window'] == 'after_checkpoint' else 'prepared'
            require(at_gate['values']['phase'] == phase and at_gate['next'] == (['verify'] if phase == 'effect_observed' else ['submit']), 'Crash window checkpoint mismatch')
            if spec['window'] == 'after_commit':
                require(at_gate['values']['ticket'] is None and at_gate['values']['create_calls'] == 0
                        and selected(gate_business) == [barrier['ticket']], 'Commit-before-checkpoint window not proved')
            if spec['window'] == 'after_checkpoint':
                require(load_json(run / 'crash/checkpoint-at-gate.json') == at_gate
                        and at_gate['values']['ticket'] == selected(gate_business)[0], 'Saved-checkpoint gate mismatch')
            sent = []
            recovery_sent = []
            for label, pid in (('crash', killed['pid']), ('recover', recovered['pid'])):
                path = run / label / 'http-client.jsonl'
                network = lines(path) if path.exists() else []
                started = [e for e in network if e['event'] == 'http_request_started']
                require(all(e['pid'] == pid for e in started), 'Wrong HTTP caller PID')
                sent += started
                if label == 'recover':
                    recovery_sent = started
            correlate = lambda rows: sorted((r['method'], r['attempt_id'], r['request_id']) for r in rows)
            require(correlate(sent) == correlate([e for e in served if e['path'] == '/tickets']), 'Client and service business requests disagree')
            expected_count = 2 if spec['window'] == 'after_commit' and spec['strategy'] == 'replay' else 1
            expected_fields = scope | manifest['case']['request'] | dict(status='open')
            require(all(all(ticket.get(k) == v for k, v in expected_fields.items()) for ticket in selected(final)), 'Final ticket content does not match original request')
            require(len(selected(final)) == record['resource_count'] == expected_count
                    and sum(r['method'] == 'POST' for r in sent) == expected_count, 'Duplicate or missing business effects')
            if spec['damage'] != 'none':
                damage = load_json(run / 'damage.json')
                require(damage['kind'] == spec['damage'] and damage['test_only'] and damage['storage'] == 'PostgresSaver', 'Unrecorded checkpoint damage')
                require(result['status'] == 'ERROR' and 'manual verification' in result['reason'].lower()
                        and not recovery_sent and job['state'] == 'waiting_verification', 'Corrupt/missing progress incorrectly resumed')
                if spec['damage'] == 'missing':
                    require(before is None and after is None, 'Deleted checkpoint silently recreated')
                else:
                    require(before['values']['environment_id'] != manifest['environment_id'] and before == after, 'Corruption not preserved/refused')
                outcome = 'ERROR'
            else:
                require(at_gate == before, 'Agent kill unexpectedly changed checkpoint')
                require(all(after['values'][k] == manifest[k] for k in IDENTITY_KEYS), 'Recovery changed bound task identity')
                for name in ('checkpoint-before', 'checkpoint-after'):
                    payload = load_json(run / 'recover' / (name + '.json'))
                    require(digest(payload['checkpoint']) == payload['sha256'], 'Recovery checkpoint digest mismatch')
                    require(payload['checkpoint'] == (before if name == 'checkpoint-before' else after), 'Recovery checkpoint read/write mismatch')
                require(after['values'] == result['values'] and after['checkpoint_id'] == job['checkpoint_id'], 'Final checkpoint pointer mismatch')
                if spec['window'] == 'after_commit' and spec['strategy'] == 'query_first':
                    reconciliation = load_json(run / 'recover/reconciliation.json')
                    require(reconciliation == result['reconciliation'] and reconciliation['consistent'] and reconciliation['tickets'] == selected(final), 'Query-first result not independently supported')
                    require(recovery_sent and all(e['method'] == 'GET' for e in recovery_sent)
                            and recovery_sent[0]['request_id'].endswith(':reconcile'), 'Query-first replayed side effect')
                    reconciled = load_json(run / 'recover/checkpoint-reconciled.json')
                    require(digest(reconciled['checkpoint']) == reconciled['sha256']
                            and checkpoint_values(database, manifest['thread_id'], reconciled['checkpoint']['checkpoint_id']) == reconciled['checkpoint']['values']
                            and reconciled['checkpoint']['values']['ticket'] == selected(final)[0], 'Reconciled progress not stored in PostgreSQL')
                if spec['window'] == 'after_checkpoint':
                    require(all(e['method'] == 'GET' for e in recovery_sent), 'Saved step unnecessarily reran')
                outcome = 'FAIL' if expected_count == 2 else 'PASS'
                require(result['status'] == ('INCONCLUSIVE' if outcome == 'FAIL' else 'PASS')
                        and job['state'] == ('waiting_verification' if outcome == 'FAIL' else 'finished'), 'Execution/business verdict mismatch')
                if outcome == 'PASS':
                    require(after['values']['phase'] == 'completed' and after['values']['ticket'] == selected(final)[0], 'Completed checkpoint differs from business truth')
            require(record['expected'] == outcome, 'Expected outcome not supported by evidence')
            require(load_json(run / 'http-cleanup.json')['exited'], 'Missing final service cleanup')
            outcomes.append(outcome)
        require(len(profiles) == 6 and all(profile == profiles[0] for profile in profiles), 'Comparison changed service, request, graph, prompt or fault')
        require(len(set(pids)) == 20, 'Worker process identity reused in this suite')
        require(outcomes.count('FAIL') == 3 and outcomes.count('PASS') == 5 and outcomes.count('ERROR') == 2, 'Unexpected repeated result distribution')
        return dict(status='PASS', experiments=10, forced_agent_kills=10, worker_processes=20,
            unsafe_duplicates=3, safe_commit_recoveries=3, business_outcomes={s: outcomes.count(s) for s in ('PASS', 'FAIL', 'ERROR')},
            model_requests=0, reason='Actual Agent kills, live services, PG checkpoints and independent SQLite prove the repeated recovery contrast')
    except Exception as exc:
        return dict(status='ERROR', reason=type(exc).__name__ + ': ' + str(exc))

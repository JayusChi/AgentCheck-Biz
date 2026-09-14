"""Test-only worker: synchronous crash gates and generation-fenced recovery."""

import argparse
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from langsmith import tracing_context
from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.adapters.http_transport import HttpTransport, HttpTimeouts
from agentcheck_biz.checks import load_json
from agentcheck_biz.events import EventLog
from agentcheck_biz.reports import save_json
from agentcheck_biz.persistence.graph import build, config, snapshot_payload, validate_snapshot, checkpointer
from agentcheck_biz.persistence.store import BindingError, IDENTITY_KEYS, digest, validate_manifest
from agentcheck_biz.scheduling.store import Scheduler
from agentcheck_biz.scheduling.worker import renewing, FencedTransport
from . import WINDOWS, STRATEGIES


def validate_spec(spec, manifest):
    validate_manifest(manifest)
    if (spec.get('protocol') != 'agent-crash/1' or spec.get('test_only') is not True
            or spec.get('window') not in WINDOWS or spec.get('strategy') not in STRATEGIES
            or spec.get('damage') not in {'none', 'missing', 'corrupt'}
            or spec.get('service_version') != 'unsafe' or spec.get('model_requests') != 0
            or spec.get('request_sha256') != manifest['request_sha256']
            or any(spec.get(k) != manifest[k] for k in IDENTITY_KEYS)):
        raise BindingError('Crash gates require an explicitly bound isolated test specification')
    return spec


def snapshot(dsn, manifest, *, validate=True):
    Scheduler(dsn).get(manifest)
    with checkpointer(dsn, readonly=True) as saver:
        state = build(saver, manifest).get_state(config(manifest))
        if not state.values:
            return None
        if validate:
            validate_snapshot(state, manifest)
        return snapshot_payload(state)


def gate(output, spec, lease, *, ticket=None):
    payload = dict(protocol='agent-crash/1', test_only=True, window=spec['window'], lease=lease,
                   job_id=spec['job_id'], thread_id=spec['thread_id'], operation_id=spec['operation_id'],
                   environment_id=spec['environment_id'], pid=os.getpid(), ticket=ticket,
                   monotonic_ns=time.monotonic_ns())
    save_json(output / 'barrier.json', payload)
    # There is no release path: only the owning controller's process kill
    # completes this experiment. Timeout raises instead of advancing the node.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        time.sleep(.05)
    raise TimeoutError('Controller did not terminate the worker at the crash gate')


class CrashTransport:
    def __init__(self, inner, output, spec, lease):
        self.inner, self.output, self.spec, self.lease = inner, output, spec, lease

    def request(self, method, *args, **kwargs):
        if method == 'POST' and self.spec['window'] == 'before_call':
            gate(self.output, self.spec, self.lease)
        result = self.inner.request(method, *args, **kwargs)
        if method == 'POST' and self.spec['window'] == 'after_commit':
            if result[0] != 200 or not isinstance(result[1], dict) or not result[1].get('ticket_id'):
                raise BindingError('Cannot establish a committed-response gate')
            gate(self.output, self.spec, self.lease, ticket=result[1])
        return result


def matches_ticket(ticket, manifest):
    expected = manifest['case']['context'] | manifest['case']['request'] | dict(status='open')
    return isinstance(ticket, dict) and bool(ticket.get('ticket_id')) and all(ticket.get(k) == v for k, v in expected.items())


def run(dsn, manifest, spec, output, request, action):
    validate_spec(spec, manifest)
    scheduler = Scheduler(dsn)
    lease = scheduler.claim(manifest, str(uuid4()), 3 if action == 'crash' else 15)
    if lease is None:
        raise BindingError('Experiment has no valid execution claim')
    save_json(output / 'claim.json', lease)
    context = RunContext(manifest['run_id'], manifest['operation_id'], lease['attempt_id'], time.time() + 90,
                         Path(manifest['evidence_dir']))
    events = EventLog(output / 'events.jsonl', context.run_id)
    raw = HttpTransport(request['origin'], context, EventLog(output / 'http-client.jsonl', context.run_id), HttpTimeouts())
    with renewing(scheduler, manifest, lease, 3 if action == 'crash' else 15) as lost, scheduler.saver(manifest, lease) as saver, tracing_context(enabled=False):
        transport = FencedTransport(scheduler, manifest, lease, raw, lost)
        actual = CrashTransport(transport, output, spec, lease) if action == 'crash' else transport
        graph = build(saver, manifest, lease['attempt_id'], actual, request['token'], events,
                      pause=action == 'crash' and spec['window'] == 'after_checkpoint')
        if action == 'crash':
            if lease['takeover']:
                raise BindingError('Initial crash process cannot start from a takeover')
            initial = {k: manifest[k] for k in IDENTITY_KEYS} | dict(attempt_id=lease['attempt_id'], phase='new',
                ticket=None, create_calls=0, query_calls=0, model_requests=0)
            graph.invoke(initial, config(manifest, lease['attempt_id']), durability='sync')
            saved = snapshot_payload(validate_snapshot(graph.get_state(config(manifest)), manifest))
            if spec['window'] != 'after_checkpoint' or saved['values']['phase'] != 'effect_observed' or saved['next'] != ['verify']:
                raise BindingError('Checkpoint-saved gate was not reached')
            save_json(output / 'checkpoint-at-gate.json', saved)
            gate(output, spec, lease, ticket=saved['values']['ticket'])
            raise AssertionError('Unreachable gate return')
        if not lease['takeover']:
            raise BindingError('Recovery requires the original expired job')
        try:
            current = graph.get_state(config(manifest))
            saved = snapshot_payload(validate_snapshot(current, manifest))
        except Exception as exc:
            scheduler.finish(manifest, lease, 'waiting_verification', None, 'checkpoint_unreadable_manual_verification')
            return dict(status='ERROR', job_state='waiting_verification', lease=lease,
                        reason='Checkpoint missing or corrupt; manual verification required', error_type=type(exc).__name__)
        save_json(output / 'checkpoint-before.json', dict(sha256=digest(saved), checkpoint=saved))
        if saved['pending_errors']:
            scheduler.finish(manifest, lease, 'waiting_verification', saved['checkpoint_id'], 'pending_error_manual_verification')
            return dict(status='ERROR', job_state='waiting_verification', lease=lease, reason='Pending error; manual verification required')
        reconciliation = None
        if spec['strategy'] == 'query_first':
            status, tickets = transport.request('GET', '/tickets', token=request['token'],
                request_id=lease['attempt_id'] + ':reconcile', operation_id=manifest['operation_id'])
            consistent = status == 200 and isinstance(tickets, list) and len(tickets) == 1 and matches_ticket(tickets[0], manifest)
            checkpoint_completed_write = saved['values']['phase'] in {'effect_observed', 'verified', 'completed'}
            if checkpoint_completed_write:
                consistent = consistent and saved['values']['ticket'] == tickets[0] and 'submit' not in saved['next']
            else:
                consistent = consistent and saved['values']['phase'] == 'prepared' and saved['next'] == ['submit'] and saved['values']['create_calls'] == 0
            reconciliation = dict(http_status=status, tickets=tickets, consistent=consistent, checkpoint_id=saved['checkpoint_id'])
            save_json(output / 'reconciliation.json', reconciliation)
            if not consistent:
                scheduler.finish(manifest, lease, 'waiting_verification', saved['checkpoint_id'], 'business_result_manual_verification')
                return dict(status='INCONCLUSIVE', job_state='waiting_verification', lease=lease,
                    reconciliation=reconciliation, reason='Business result uncertain; manual verification required')
            if not checkpoint_completed_write:
                # Persist the actual queried result through the official saver.
                # Mark submit completed; never manufacture a final JSON outcome.
                graph.update_state(config(manifest, lease['attempt_id']),
                    dict(ticket=tickets[0], phase='effect_observed', create_calls=1, attempt_id=lease['attempt_id']), as_node='submit')
                reconciled = snapshot_payload(validate_snapshot(graph.get_state(config(manifest)), manifest))
                save_json(output / 'checkpoint-reconciled.json', dict(sha256=digest(reconciled), checkpoint=reconciled))
                events.record('checkpoint_reconciled_from_query', checkpoint_id=reconciled['checkpoint_id'], ticket=tickets[0])
        error = None
        try:
            graph.invoke(None, config(manifest, lease['attempt_id']), durability='sync')
        except Exception as exc:
            error = type(exc).__name__
        saved = snapshot_payload(validate_snapshot(graph.get_state(config(manifest)), manifest))
        save_json(output / 'checkpoint-after.json', dict(sha256=digest(saved), checkpoint=saved))
        state = 'waiting_verification' if error or saved['next'] else 'finished'
        scheduler.finish(manifest, lease, state, saved['checkpoint_id'], error or 'recovery_completed')
        return dict(status='INCONCLUSIVE' if error else 'PASS', job_state=state, lease=lease,
                    error_type=error, reconciliation=reconciliation, **saved)


def main():
    parser = argparse.ArgumentParser(description='D28 isolated Agent crash worker; explicit test specification required')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--test-config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--action', choices=('crash', 'recover'), required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        request = json.loads(__import__('sys').stdin.readline())
        manifest, spec = load_json(args.manifest), load_json(args.test_config)
        if args.test_config.resolve().parent != Path(manifest['evidence_dir']).resolve():
            raise BindingError('Test specification must belong to the isolated environment')
        result = run(request['dsn'], manifest, spec, args.output, request, args.action)
    except Exception as exc:
        result = dict(status='ERROR', error_type=type(exc).__name__, reason='Experiment failed; manual verification required')
    result.update(pid=os.getpid(), model_requests=0)
    save_json(args.output / 'result.json', result)
    print(json.dumps(result, ensure_ascii=False))
    return {'PASS': 0, 'INCONCLUSIVE': 2, 'ERROR': 3}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())

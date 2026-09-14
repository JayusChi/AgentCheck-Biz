"""Finite worker entry point; credentials enter through stdin, never CLI/logs."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
from threading import Event, Thread
import time
from uuid import uuid4

from langsmith import tracing_context
from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.adapters.http_transport import HttpTransport, HttpTimeouts
from agentcheck_biz.events import EventLog
from agentcheck_biz.checks import load_json
from agentcheck_biz.reports import save_json
from agentcheck_biz.persistence.graph import build, config, snapshot_payload, validate_snapshot, checkpointer
from agentcheck_biz.persistence.store import IDENTITY_KEYS, Store, BindingError
from .store import Scheduler, LeaseLost


@contextmanager
def renewing(scheduler, manifest, lease, seconds):
    stop, lost = Event(), Event()
    def loop():
        while not stop.wait(seconds / 3):
            try:
                scheduler.heartbeat(manifest, lease, seconds)
            except Exception:
                lost.set()
                return
    thread = Thread(target=loop, name='lease-heartbeat', daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        stop.set()
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError('Heartbeat thread did not stop')


class FencedTransport:
    def __init__(self, scheduler, manifest, lease, transport, lost):
        self.scheduler, self.manifest, self.lease = scheduler, manifest, lease
        self.transport, self.lost = transport, lost

    def request(self, *args, **kwargs):
        if self.lost.is_set():
            raise LeaseLost('Heartbeat failed')
        with self.scheduler.guarded(self.manifest, self.lease):
            pass
        # Do not hold database locks across HTTP. A request already sent may
        # commit after lease loss; takeover must still verify external truth.
        return self.transport.request(*args, **kwargs)


def execute(scheduler, manifest, lease, output, request, *, pause=False, gap=False, seconds=30):
    context = RunContext(manifest['run_id'], manifest['operation_id'], lease['attempt_id'], time.time() + 60,
                         Path(manifest['evidence_dir']))
    events = EventLog(output / 'events.jsonl', context.run_id)
    raw = HttpTransport(request['origin'], context, EventLog(output / 'http-client.jsonl', context.run_id), HttpTimeouts())
    with renewing(scheduler, manifest, lease, seconds) as lost, scheduler.saver(manifest, lease) as saver, tracing_context(enabled=False):
        transport = FencedTransport(scheduler, manifest, lease, raw, lost)
        graph = build(saver, manifest, lease['attempt_id'], transport, request['token'], events, pause=pause, gap=gap)
        reconciliation = None
        if lease['takeover']:
            snapshot = graph.get_state(config(manifest))
            saved = snapshot_payload(validate_snapshot(snapshot, manifest)) if snapshot.values else None
            status, tickets = transport.request('GET', '/tickets', token=request['token'],
                request_id=lease['attempt_id'] + ':reconcile', operation_id=manifest['operation_id'])
            safe = (status == 200 and saved is not None and not saved['pending_errors']
                    and saved['values']['phase'] in {'effect_observed', 'verified', 'completed'}
                    and saved['values']['create_calls'] == 1 and tickets == [saved['values']['ticket']]
                    and 'submit' not in saved['next'])
            reconciliation = dict(checkpoint=saved, http_status=status, business_tickets=tickets, safe_to_resume=safe)
            save_json(output / 'reconciliation.json', reconciliation)
            events.record('business_reconciled', generation=lease['generation'], **reconciliation)
            if not safe:
                scheduler.finish(manifest, lease, 'waiting_verification', saved['checkpoint_id'] if saved else None,
                                 'external_result_or_checkpoint_requires_manual_verification')
                return dict(status='INCONCLUSIVE', job_state='waiting_verification', lease=lease,
                            reconciliation=reconciliation, model_requests=0, pid=os.getpid())
            initial = None
        else:
            initial = {k: manifest[k] for k in IDENTITY_KEYS} | dict(attempt_id=lease['attempt_id'], phase='new',
                        ticket=None, create_calls=0, query_calls=0, model_requests=0)
        error = None
        try:
            graph.invoke(initial, config(manifest, lease['attempt_id']), durability='sync')
        except Exception as exc:
            error = type(exc).__name__
        saved = snapshot_payload(validate_snapshot(graph.get_state(config(manifest)), manifest))
        if not pause:
            state = 'waiting_verification' if error or saved['next'] else 'finished'
            scheduler.finish(manifest, lease, state, saved['checkpoint_id'], error or 'verified_checkpoint_completed')
        else:
            state = 'running'  # Deliberately abandon; no release or artificial expiry.
        return dict(status='INCONCLUSIVE' if error else 'PASS', job_state=state, lease=lease, error_type=error,
                    reconciliation=reconciliation, pid=os.getpid(), model_requests=0, **saved)


def stale_probes(scheduler, manifest, lease):
    probes = {}
    def attempt(name, operation):
        try:
            operation()
        except (BindingError, __import__('psycopg').errors.CheckViolation):
            probes[name] = 'REJECTED'
        else:
            probes[name] = 'ACCEPTED'
    attempt('heartbeat', lambda: scheduler.heartbeat(manifest, lease))
    attempt('status', lambda: scheduler.finish(manifest, lease, 'finished', 'stale', 'stale'))
    attempt('legacy_status', lambda: Store(scheduler.dsn).finish_attempt(manifest, lease['attempt_id'], 'finished', 'stale', 'stale'))
    attempt('legacy_begin', lambda: Store(scheduler.dsn).begin(manifest))
    def checkpoint(fenced):
        manager = scheduler.saver(manifest, lease) if fenced else checkpointer(scheduler.dsn)
        with manager as saver:
            build(saver, manifest).update_state(config(manifest), dict(phase='stale_corruption'))
    attempt('fenced_checkpoint', lambda: checkpoint(True))
    attempt('legacy_checkpoint', lambda: checkpoint(False))
    return probes


def wait_file(path, timeout=60):
    deadline = time.monotonic() + timeout
    while not Path(path).is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError('Synchronization gate timed out')
        time.sleep(0.025)


def main():
    parser = argparse.ArgumentParser(description='D27 durable queue worker, one job per invocation')
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--action', choices=['run', 'pause', 'gap', 'zombie', 'claim', 'heartbeat'], default='run')
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--barrier', type=Path)
    parser.add_argument('--release', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        request = json.loads(__import__('sys').stdin.readline())
        manifest = load_json(args.manifest)
        scheduler = Scheduler(request['dsn'])
        if args.barrier:
            save_json(args.output / 'ready.json', dict(pid=os.getpid()))
            wait_file(args.barrier)
        if args.action == 'heartbeat':
            result = dict(status='PASS', renewed_until=scheduler.heartbeat(manifest, request['lease'], args.seconds))
        else:
            lease = scheduler.claim(manifest, str(uuid4()), args.seconds)
            if lease is None:
                result = dict(status='BUSY')
            elif args.action == 'claim':
                result = dict(status='PASS', lease=lease)
            else:
                result = execute(scheduler, manifest, lease, args.output, request,
                    pause=args.action in {'pause', 'gap', 'zombie'}, gap=args.action == 'gap', seconds=args.seconds)
                if args.action == 'zombie':
                    save_json(args.output / 'paused.json', result)
                    wait_file(args.release)
                    before = scheduler.export()
                    probes = stale_probes(scheduler, manifest, lease)
                    result.update(probes=probes, stale_writes_unchanged=before == scheduler.export())
                    if set(probes.values()) != {'REJECTED'} or not result['stale_writes_unchanged']:
                        result['status'] = 'ERROR'
        result.update(pid=os.getpid(), model_requests=0)
    except Exception as exc:
        result = dict(status='ERROR', error_type=type(exc).__name__, pid=os.getpid(), model_requests=0)
    save_json(args.output / 'result.json', result)
    print(json.dumps(result, ensure_ascii=False))
    return {'PASS': 0, 'BUSY': 0, 'INCONCLUSIVE': 2, 'ERROR': 3}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())

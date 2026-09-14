"""Real process entry: checkpointed ledger workflow and lease-fenced takeover."""
import argparse
import json
import os
from pathlib import Path
from typing import TypedDict
from uuid import uuid4
from langgraph.graph import StateGraph, START, END
from langsmith import tracing_context
from agentcheck_biz.checks import load_json
from agentcheck_biz.persistence.graph import config, snapshot_payload, validate_snapshot
from agentcheck_biz.persistence.store import IDENTITY_KEYS, BindingError, digest
from agentcheck_biz.scheduling.worker import renewing
from agentcheck_biz.agent_crash.worker import gate
from agentcheck_biz.reports import save_json
from .store import Operations
from .policy import Recovery, NeedsVerification
from .targets import Ticket, Gitea


class State(TypedDict):
    job_id: str
    experiment_id: str
    environment_id: str
    thread_id: str
    run_id: str
    operation_id: str
    evidence_dir: str
    request_sha256: str
    storage_version: int
    graph_version: str
    phase: str
    result: dict | None
    model_requests: int


def graph(saver, recovery=None, pause=False):
    flow = StateGraph(State)
    flow.add_node('prepare', lambda state: dict(phase='prepared'))
    flow.add_node('apply', lambda state: dict(phase='confirmed', result=recovery.apply()))
    flow.add_node('finish', lambda state: dict(phase='completed'))
    for a, b in ((START,'prepare'),('prepare','apply'),('apply','finish'),('finish',END)):
        flow.add_edge(a,b)
    return flow.compile(checkpointer=saver, interrupt_after=['apply'] if pause else None)


def run(manifest, output, connection, action, test):
    store = Operations(connection['dsn'])
    store.get(manifest)
    if type(connection.get('retry_unknown', False)) is not bool:
        raise BindingError('Replay selection must be an explicit boolean')
    if test and (test.get('test_only') is not True or test.get('job_id') != manifest['job_id']
                 or test.get('window') not in {'before_call','sent_unknown','after_commit','after_checkpoint'}):
        raise BindingError('Crash gate requires explicit isolated test binding')
    lease = store.claim(manifest, str(uuid4()), 3 if action == 'crash' else 30)
    if lease is None:
        row, job = store.read(manifest), store.get(manifest)
        if row and row['state'] == 'confirmed' and job['state'] == 'finished':
            return dict(status='PASS', cached=True, result=row['result'], job_state=job['state'])
        return dict(status='BUSY', reason='Another execution owns the job or manual verification is required')
    save_json(output/'claim.json', lease)
    checkpoint_id = None
    with renewing(store, manifest, lease, 3 if action == 'crash' else 30), store.saver(manifest, lease) as saver, tracing_context(enabled=False):
        try:
            if action == 'crash' and (lease['takeover'] or not test):
                raise BindingError('Crash experiment must start a fresh bound job')
            if action == 'recover':
                saved = snapshot_payload(validate_snapshot(graph(saver).get_state(config(manifest)), manifest))
                checkpoint_id = saved['checkpoint_id']
                save_json(output/'checkpoint-before.json', dict(checkpoint=saved, sha256=digest(saved)))
                if not lease['takeover'] or store.read(manifest) is None:
                    raise BindingError('Recovery requires the original ledger and expired execution')
            else:
                store.prepare(manifest, lease)
            target = (Ticket if manifest['side_effect']['kind'] == 'ticket' else Gitea)(manifest, lease, output, connection)
            def synchronize(window):
                if action == 'crash' and window == test['window']:
                    gate(output, manifest | dict(window=window), lease)
            recovery = Recovery(store, manifest, lease, target, gate=synchronize,
                                retry_unknown=connection.get('retry_unknown', False))
            flow = graph(saver, recovery, pause=action == 'crash' and test['window'] == 'after_checkpoint')
            initial = {k:manifest[k] for k in IDENTITY_KEYS} | dict(phase='new', result=None, model_requests=0)
            flow.invoke(None if action == 'recover' else initial, config(manifest, lease['attempt_id']), durability='sync')
            saved = snapshot_payload(validate_snapshot(flow.get_state(config(manifest)), manifest))
            checkpoint_id = saved['checkpoint_id']
            save_json(output/'checkpoint-after.json', dict(checkpoint=saved, sha256=digest(saved)))
            if action == 'crash':
                synchronize('after_checkpoint')
                raise BindingError('Expected crash gate was not reached')
            ledger = store.read(manifest)
            if saved['next'] or ledger['state'] != 'confirmed' or saved['values']['result'] != ledger['result']:
                raise BindingError('Completion requires confirmed ledger and complete graph')
            store.finish(manifest, lease, 'finished', checkpoint_id, 'operation_confirmed')
            return dict(status='PASS', job_state='finished', lease=lease, result=saved['values']['result'])
        except Exception as exc:
            store.finish(manifest, lease, 'waiting_verification', checkpoint_id, 'operation_manual_verification')
            return dict(status='INCONCLUSIVE' if isinstance(exc, NeedsVerification) else 'ERROR',
                job_state='waiting_verification', lease=lease, reason=str(exc), error_type=type(exc).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--action', choices=('start','crash','recover'), required=True)
    parser.add_argument('--test-config', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        manifest = load_json(args.manifest)
        if args.test_config and args.test_config.resolve().parent != Path(manifest['evidence_dir']).resolve():
            raise BindingError('Test configuration must belong to the isolated job')
        result = run(manifest, args.output, json.loads(__import__('sys').stdin.readline()), args.action,
                     load_json(args.test_config) if args.test_config else None)
    except Exception as exc:
        result = dict(status='ERROR', reason=str(exc), error_type=type(exc).__name__)
    result.update(pid=os.getpid(), model_requests=0)
    save_json(args.output/'result.json', result)
    print(json.dumps(result, ensure_ascii=False))
    return dict(PASS=0, BUSY=2, INCONCLUSIVE=2, ERROR=3)[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())

"""Finite offline Agent process. Connection secrets enter only through stdin."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import time
from uuid import uuid4
from langsmith import tracing_context
from agentcheck_biz.checks import load_json
from agentcheck_biz.events import EventLog
from agentcheck_biz.persistence.graph import config,snapshot_payload
from agentcheck_biz.persistence.store import IDENTITY_KEYS,BindingError,digest
from agentcheck_biz.scheduling.worker import renewing
from agentcheck_biz.reports import save_json
from .store import Budgets,Stopped
from .graph import graph,validate


def run(manifest,output,connection,action,test=None):
    store=Budgets(connection['dsn'])
    job=store.get(manifest)
    row=store.read(manifest)
    if job['state']=='finished' and row and row['state']=='confirmed':
        return dict(status='PASS',cached=True,result=row['result'])
    if action!='recover' and job['state']!='queued':
        raise BindingError('Existing jobs must resume; never restart with fresh state')
    if action=='recover' and job['state']=='queued':
        raise BindingError('Resume requires an existing checkpoint and attempt')
    if test and (test.get('test_only') is not True or test.get('job_id')!=manifest['job_id'] or
                 test.get('window') not in {'after_model','model_reserved','after_commit','sent_unknown'}):
        raise BindingError('Test gate must be bound to an isolated job')
    seconds=3 if action in {'crash','zombie'} else 30
    lease=store.claim(manifest,str(uuid4()),seconds)
    if lease is None:return dict(status='BUSY')
    save_json(output/'claim.json',lease)
    events=EventLog(output/'dispatch.jsonl',manifest['run_id'])
    pointer=None
    def gate(window):
        if action not in {'crash','hold','zombie'} or not test or test['window']!=window:return
        save_json(output/'barrier.json',dict(test_only=True,window=window,pid=os.getpid(),lease=lease,
            job_id=manifest['job_id'],thread_id=manifest['thread_id'],operation_id=manifest['operation_id']))
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            if action in {'hold','zombie'} and (output/'release.json').is_file():
                release=load_json(output/'release.json')
                if release != dict(test_only=True,job_id=manifest['job_id'],pid=os.getpid()):
                    raise BindingError('Foreign test release')
                return
            time.sleep(.03)
        raise TimeoutError('Owned controller did not resolve test gate')
    try:
        with (nullcontext() if action=='zombie' else renewing(store,manifest,lease,seconds)), store.saver(manifest,lease) as saver, tracing_context(enabled=False):
            if action=='recover':
                saved=graph(saver,manifest).get_state(config(manifest))
                validate(saved.values,manifest)
                if not lease['takeover'] or store.read(manifest) is None:
                    raise BindingError('Resume requires the original ledger and checkpoint')
                pointer=saved.config['configurable']['checkpoint_id']
                payload=snapshot_payload(saved)
                save_json(output/'checkpoint-before.json',dict(checkpoint=payload,sha256=digest(payload)))
            else:
                store.prepare(manifest,lease)
            paused=bool(test and test['window']=='after_model' and action!='recover')
            flow=graph(saver,manifest,store,lease,output,connection,events,gate=gate,pause=paused)
            initial={k:manifest[k] for k in IDENTITY_KEYS}|dict(recovery_version='continuity/1',phase='new',messages=[],result=None,model_requests=0)
            flow.invoke(None if action=='recover' else initial,config(manifest,lease['attempt_id']),durability='sync')
            if paused:
                gate('after_model')
                flow.invoke(None,config(manifest,lease['attempt_id']),durability='sync')
            saved=flow.get_state(config(manifest))
            validate(saved.values,manifest)
            payload=snapshot_payload(saved)
            pointer=payload['checkpoint_id']
            save_json(output/'checkpoint-after.json',dict(checkpoint=payload,sha256=digest(payload)))
            if saved.next or saved.values['phase']!='completed' or saved.values['result']!=store.read(manifest)['result']:
                raise BindingError('Completion requires the confirmed ToolMessage and complete graph')
            store.finish(manifest,lease,'finished',pointer,'offline_agent_and_business_confirmed')
            return dict(status='PASS',lease=lease,result=saved.values['result'])
    except Exception as exc:
        try:
            store.finish(manifest,lease,'waiting_verification',pointer,'recovery_requires_verification')
        except Exception:
            pass  # Revoked leases / unavailable storage must not regain write access.
        return dict(status='INCONCLUSIVE',lease=lease,error_type=type(exc).__name__,reason=str(exc))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--action',required=True,choices=('start','recover','crash','hold','zombie'))
    parser.add_argument('--test-config',type=Path)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    try:
        m=load_json(args.manifest)
        if args.test_config and args.test_config.resolve().parent!=Path(m['evidence_dir']).resolve():
            raise BindingError('Test configuration must belong to the job')
        result=run(m,args.output,json.loads(__import__('sys').stdin.readline()),args.action,
                   load_json(args.test_config) if args.test_config else None)
    except Exception as exc:
        result=dict(status='INCONCLUSIVE' if isinstance(exc,Stopped) else 'ERROR',error_type=type(exc).__name__,reason=str(exc))
    result.update(pid=os.getpid(),model_requests=0)
    save_json(args.output/'result.json',result)
    print(json.dumps(result,ensure_ascii=False))
    return dict(PASS=0,BUSY=2,INCONCLUSIVE=2,ERROR=3)[result['status']]


if __name__=='__main__':raise SystemExit(main())

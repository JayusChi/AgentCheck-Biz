"""Preallocated slots, atomic snapshots and isolated finite native workers."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
from agentcheck_biz.persistence.store import digest
from agentcheck_biz.persistence.runtime import child_environment
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.checks import load_json
from agentcheck_biz.network_acceptance.verify import hashes
from .manifest import validate,source_digest
from .evidence import aggregate,inspect
from . import EVIDENCE_VERSION


def save(path,value):
    path=Path(path)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    with temporary.open('w',encoding='utf8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(8):
        try:
            os.replace(temporary,path)
            break
        except PermissionError:
            if attempt==7:raise
            time.sleep(.01*(attempt+1))


def now():return datetime.now(timezone.utc).isoformat()


def allocate(manifest,output):
    validate(manifest)  # ALL cases/capabilities before creating ANY batch directory.
    directory=Path(output).resolve()/('batch-'+uuid4().hex)
    directory.mkdir(parents=True)
    slots=[]
    for spec in manifest['slots']:
        slot=dict(slot_id=spec['slot_id'],profile=spec['profile'],state='not_started',matched=False,
                  events=[dict(sequence=0,state='not_started',time=now())])
        (directory/'slots'/spec['slot_id']/'payload').mkdir(parents=True)
        save(directory/'slots'/spec['slot_id']/'slot.json',slot)
        slots.append(slot)
    save(directory/'plan.json',dict(evidence_version=EVIDENCE_VERSION,manifest=manifest))
    report=dict(evidence_version=EVIDENCE_VERSION,batch_id=directory.name,manifest_sha256=digest(manifest),
                status='RUNNING',created_at=now(),slots=slots,aggregate=aggregate(slots),model_requests=0)
    save(directory/'summary.json',report)
    return directory,report


def execute_slot(directory,spec):
    slot_dir=directory/'slots'/spec['slot_id']
    args=['-m','agentcheck_biz.v2','_worker','--manifest',str(directory/'plan.json'),
          '--slot',spec['slot_id'],'--output',str(slot_dir/'payload')]
    env=child_environment(dict(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',__PYVENV_LAUNCHER__=sys.executable))
    start=time.time()
    with (slot_dir/'worker.log').open('wb') as log:
        process=subprocess.Popen([sys._base_executable,'-X','utf8',*args],cwd=REPO_ROOT,env=env,
            stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        try:code=process.wait(timeout=spec['budget']['slot_timeout_seconds'])
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            code=124
    receipt=dict(pid=process.pid,exited=process.poll() is not None,exit_code=code,started_at=start,finished_at=time.time())
    result=dict(process=receipt,state='error',matched=False,evidence_sha256=hashes(slot_dir/'payload'))
    if code==0:
        try:
            actual=inspect(slot_dir/'payload',spec)
            result.update(state='completed',inspection=actual,
                          matched=actual['oracle_ok'] and all(actual['observed'].get(k)==v for k,v in spec['expected'].items()))
        except Exception as error:result['error']=type(error).__name__+': '+str(error)
    else:result['error']='Slot process failed or timed out; partial evidence retained'
    return result


def batch(manifest,output):
    directory,report=allocate(manifest,output)
    from .process import own_descendants
    job=own_descendants()
    by_id={s['slot_id']:s for s in report['slots']}
    def persist():
        report['aggregate']=aggregate(report['slots'])
        save(directory/'summary.json',report)
    def transition(slot,state,**details):
        slot.update(details,state=state)
        slot['events'].append(dict(sequence=len(slot['events']),state=state,time=now()))
        save(directory/'slots'/slot['slot_id']/'slot.json',slot)
        persist()
    # Submit bounded waves. Unsubmitted slots stay explicitly not_started.
    parallel=manifest['execution']['parallel']
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for offset in range(0,len(manifest['slots']),parallel):
            futures={}
            for spec in manifest['slots'][offset:offset+parallel]:
                transition(by_id[spec['slot_id']],'started')
                futures[pool.submit(execute_slot,directory,spec)]=spec
            for future in as_completed(futures):
                slot=by_id[futures[future]['slot_id']]
                try:
                    result=future.result()
                    state=result.pop('state')
                    transition(slot,state,**result)
                except Exception as error:transition(slot,'error',error=type(error).__name__+': '+str(error))
                print(slot['slot_id']+': '+slot['state'],file=sys.stderr,flush=True)
    report['status']='PASS' if report['slots'] and all(s['state']=='completed' and s['matched'] for s in report['slots']) else 'FAIL'
    report['finished_at']=now()
    persist()
    return dict(status=report['status'],directory=str(directory),aggregate=report['aggregate'],model_requests=0)


def worker(plan_path,slot_id,output):
    from .process import own_descendants
    job=own_descendants()
    manifest=load_json(plan_path)['manifest']
    validate(manifest)  # Recheck source and frozen case immediately before execution.
    slot=next(s for s in manifest['slots'] if s['slot_id']==slot_id)
    output=Path(output).resolve()
    if output != Path(plan_path).resolve().parent/'slots'/slot_id/'payload':
        raise ValueError('Worker output does not match its preallocated slot')
    save(output/'worker-identity.json',dict(pid=os.getpid(),slot_id=slot_id,manifest_sha256=digest(manifest),
                                         owned_descendant_job=bool(job),model_requests=0))
    if slot['engine']=='network':
        from agentcheck_biz.network_acceptance.runner import execute
        from agentcheck_biz.network_acceptance.profiles import profiles
        profile=next(p for p in profiles() if 'network:'+p['id']==slot['profile'])
        result=execute(profile,output)
        run=Path(result['run_dir'])
    else:
        from agentcheck_biz.continuity.demo import suite
        result=suite(output,scenario_names=[slot['profile'].split(':',1)[1]])
        if result['status']!='PASS':raise ValueError('Continuity execution failed; retained summary')
        run=Path(result['summary_path']).parent
    if source_digest()!=manifest['source_sha256']:
        raise ValueError('Source changed during slot execution; retain partial evidence')
    save(output/'outcome.json',dict(slot_id=slot_id,profile=slot['profile'],run=run.relative_to(output).as_posix()))
    return dict(status='PASS',slot_id=slot_id)

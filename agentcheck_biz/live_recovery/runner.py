"""Six immutable slots; controller owns services, worker termination and evidence."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.adapters.http_transport import HttpTimeouts
from agentcheck_biz.adapters.ticket_http import TicketHttpEnvironment
from agentcheck_biz.checks import load_json, observe_database
from agentcheck_biz.events import EventLog
from agentcheck_biz.persistence.graph import checkpointer, config, snapshot_payload
from agentcheck_biz.persistence.runtime import PostgresRuntime
from agentcheck_biz.persistence.store import new_manifest, digest
from agentcheck_biz.reports import save_json
from agentcheck_biz.scheduling.demo import wait_expiry
from agentcheck_biz.side_effects.demo import spawn
from .plan import validate, LIMITS
from .proxy import LossProxy
from .worker import LiveBudgets, build

TOKEN_KEYS=('prompt_tokens','completion_tokens','total_tokens')


def inspect(directory, plan, slot):
    """Read independent business DB; model statements are never business evidence."""
    directory=Path(directory)
    row=slot|dict(status='ERROR',observation_complete=False,model_requests=0,unknown_usage_requests=0,
                  known_usage={k:0 for k in TOKEN_KEYS},cost=None,cost_source='provider bill not available',
                  fault_covered=False,recovery_triggered=False,duplicate=None,false_completion=None)
    budget=load_json(directory/'budget.json')
    records=[load_json(p) for p in directory.glob('*/model-*.json')]
    known=[r['usage'] for r in records if isinstance(r.get('usage'),dict)
        and all(type(r['usage'].get(k)) is int and r['usage'][k]>=0 for k in TOKEN_KEYS)]
    mode=load_json(directory/'manifest.json')['mode']
    row.update(model_requests=budget['model_calls'] if mode=='live' else 0,
        budget_model_dispatches=budget['model_calls'],mode=mode,tool_calls=budget['tool_calls'],
        known_usage={k:sum(r[k] for r in known) for k in TOKEN_KEYS},
        unknown_usage_requests=budget['model_calls']-len(known),
        response_models=sorted({r['response_model'] for r in records if r.get('response_model')}),
        recovery_triggered=(directory/'start/termination.json').is_file(),
        deadline=budget['deadline'],budget_events=budget['events'])
    observed=observe_database(directory/'business.sqlite')
    initial=load_json(directory/'initial.json')
    manifest=load_json(directory/'manifest.json')
    if observed['run_id']!=directory.name or initial['run_id']!=directory.name:
        raise ValueError('Business database identity mismatch')
    context=plan['case']['context']
    matching=[r for r in observed['tickets'] if all(r[k]==v for k,v in context.items())]
    unrelated=[r for r in observed['tickets'] if r not in matching]
    initial_unrelated=[r for r in initial['tickets'] if not all(r[k]==v for k,v in context.items())]
    result=load_json(directory/'worker-result.json')
    answer=result.get('answer',{})
    supported=(len(matching)==1 and all(matching[0][k]==v for k,v in plan['case']['request'].items())
               and matching[0]['status']=='open' and matching[0]['ticket_id']==answer.get('ticket_id'))
    structured=answer.get('status')=='completed'
    row.update(observation_complete=True,ticket_count=len(matching),duplicate=len(matching)>1,
        structured_completion=structured,false_completion=not supported if structured else None,
        unrelated_unchanged=unrelated==initial_unrelated,answer=answer)
    if slot['family']=='response-loss':
        events=[json.loads(line) for line in (directory/'loss-proxy.jsonl').read_text(encoding='utf8').splitlines()]
        row['fault_covered']=any(e['event']=='committed_response_dropped' for e in events)
    elif row['recovery_triggered']:
        kill=load_json(directory/'start/termination.json')
        gate=load_json(directory/'at-crash.json')
        saved=load_json(directory/'recover/checkpoint-before.json')
        row['fault_covered']=(kill['exited'] and kill['exit_code']!=0 and gate['committed_count']==1
            and gate['checkpoint']==saved and bool(saved['next']))
    row['retry_create_calls']=sum(e['kind']=='tool' and e['detail'].get('name')=='create_ticket' for e in budget['events'])
    row['fix_exercised']=slot['phase']=='C' and row['fault_covered'] and row['retry_create_calls']>1
    if result['status'] in {'ERROR','INCONCLUSIVE'}:row.update(status=result['status'],error_type=result.get('error_type'))
    elif not unrelated==initial_unrelated or len(matching)>1 or row['false_completion']:row['status']='FAIL'
    elif slot['fault'] and not row['fault_covered']:row['status']='INCONCLUSIVE'
    elif not structured:row['status']='INCONCLUSIVE'
    else:row['status']='PASS' if supported else 'FAIL'
    if manifest['experiment_plan']!=plan or manifest['slot']!=slot or budget['model_calls']>5 or budget['tool_calls']>6:
        row['status']='ERROR'
    return row


def execute_slot(directory,plan,slot,store,mode,model_key=None,retry=False):
    directory.mkdir()
    deadline=time.monotonic()+180
    ctx=RunContext(directory.name,plan['case']['context']['operation_id'],'environment',time.time()+180,directory)
    events=EventLog(directory/'environment.jsonl',directory.name)
    service=TicketHttpEnvironment(plan['case'],slot['app_version'],HttpTimeouts())
    proxy=None; workers=[]; manifest=None
    result=dict(status='ERROR',error_type='SlotNotFinished')
    try:
        service.prepare(ctx,events)
        save_json(directory/'initial.json',observe_database(directory/'business.sqlite'))
        manifest=new_manifest(directory,plan['case'])|dict(experiment_plan=plan,slot=slot,mode=mode,
            recovery=LIMITS|dict(version='continuity/1',client='d34-live/1'))
        save_json(directory/'manifest.json',manifest)
        store.create(manifest)
        proxy=LossProxy(service,directory,ctx,slot['family']=='response-loss' and slot['fault'])
        connection=dict(dsn=store.dsn,origin=proxy.origin,token=service.tokens['tenant-A'],
            mode=mode,model_key=model_key,retry=retry)
        def start(action,label):
            log=(directory/(label+'.log')).open('wb')
            process=spawn('agentcheck_biz.live_recovery.worker',[str(directory/'manifest.json'),str(directory/label),action],connection,log)
            workers.append((process,log,label)); return process
        crash=slot['family']=='agent-crash' and slot['fault']
        process=start('crash' if crash else 'start','start')
        while process.poll() is None:
            if time.monotonic()>=deadline:raise TimeoutError('Parent slot timeout')
            barrier=directory/'start/barrier.json'
            if crash and barrier.is_file():
                gate=load_json(barrier)
                if gate['pid']!=process.pid or gate['job_id']!=manifest['job_id']:raise ValueError('Foreign crash barrier')
                rows=observe_database(directory/'business.sqlite')
                matching=[r for r in rows['tickets'] if all(r[k]==v for k,v in plan['case']['context'].items())]
                if len(matching)!=1 or matching[0]!=gate['ticket']:raise ValueError('Commit not independently visible')
                with checkpointer(store.dsn,readonly=True) as saver:
                    checkpoint=snapshot_payload(build(saver,manifest).get_state(config(manifest)))
                save_json(directory/'at-crash.json',dict(checkpoint=checkpoint,committed_count=len(matching),business=rows,budget=store.budget(manifest)))
                process.kill(); process.wait(timeout=5)
                save_json(directory/'start/termination.json',dict(pid=process.pid,exited=True,exit_code=process.returncode,barrier_sha256=digest(gate),termination='owned_Popen.kill'))
                wait_expiry(store,manifest)
                process=start('recover','recover'); crash=False
            time.sleep(.03)
        label=workers[-1][2]
        result=load_json(directory/label/'result.json')
        if result['pid']!=process.pid or process.returncode!=dict(PASS=0,FAIL=1,INCONCLUSIVE=2,ERROR=3)[result['status']]:
            raise ValueError('Worker process receipt mismatch')
    except Exception as exc:
        result=dict(status='ERROR',error_type=type(exc).__name__)
    finally:
        for process,log,label in workers:
            if process.poll() is None:process.kill()
            process.wait(timeout=5); log.close()
            save_json(directory/(label+'-cleanup.json'),dict(pid=process.pid,exited=True,exit_code=process.returncode))
        if manifest is not None:
            save_json(directory/'budget.json',store.budget(manifest))
            save_json(directory/'postgres-export.json',store.export())
        if proxy:proxy.close()
        service.cleanup(ctx,events)
        save_json(directory/'worker-result.json',result)
    return inspect(directory,plan,slot)


def batch(directory,plan,mode,authorization=None,model_key=None,retry=False):
    validate(plan)
    if mode not in {'live','offline-test'}:raise ValueError('Explicit execution mode required')
    if mode=='live':
        from .plan import authorize
        authorize(plan,authorization or {},directory)
        if retry or not model_key:raise ValueError('Live mode requires credentials and forbids test fixtures')
    directory=Path(directory).resolve(); directory.mkdir(parents=True,exist_ok=False)
    save_json(directory/'plan.json',plan)
    if authorization:save_json(directory/'authorization.json',authorization)
    report=dict(schema='d34-results/1',mode=mode,status='RUNNING',plan_sha256=digest(plan),
        started_at=datetime.now(timezone.utc).isoformat(),reserved_requests=0,model_requests=0,
        slots=[s|dict(status='NOT_STARTED') for s in plan['slots']],
        limitations=['One sample per cell; no success-rate or general reliability claim.',
            'Same recovery adapter across A/B/C; only service changes B/C.',
            'No create retry means atomic deduplication was not exercised.',
            'Known tokens are partial; unknown usage is not zero; cost requires provider bill.'])
    save_json(directory/'summary.json',report)
    pg=PostgresRuntime(directory/'postgres')
    try:
        with pg:
            store=LiveBudgets(pg.dsn); store.setup()
            for index,slot in enumerate(plan['slots']):
                validate(plan)
                report['reserved_requests']+=5
                report['slots'][index].update(status='INCONCLUSIVE',reason='Started; result pending')
                save_json(directory/'summary.json',report)
                print(slot['slot_id']+' starting',flush=True)
                try:row=execute_slot(directory/slot['slot_id'],plan,slot,store,mode,model_key,retry)
                except Exception as exc:row=slot|dict(status='ERROR',error_type=type(exc).__name__,request_count_uncertain=True)
                report['slots'][index]=row
                report['model_requests']=sum(s.get('model_requests',0) for s in report['slots'])
                report['budget_model_dispatches']=sum(s.get('budget_model_dispatches',0) for s in report['slots'])
                save_json(directory/'summary.json',report)
                print(slot['slot_id']+': '+row['status'],flush=True)
                if row['status'] in {'ERROR','INCONCLUSIVE'}:break
        statuses=[s['status'] for s in report['slots']]
        report['status']=next((s for s in ('ERROR','INCONCLUSIVE','FAIL') if s in statuses),'PASS')
    except Exception as exc:report.update(status='ERROR',error_type=type(exc).__name__)
    report.update(postgres_cleanup=pg.receipts,finished_at=datetime.now(timezone.utc).isoformat(),
        counts={s:sum(r['status']==s for r in report['slots']) for s in ('PASS','FAIL','ERROR','INCONCLUSIVE','NOT_STARTED')},
        known_usage={k:sum(r.get('known_usage',{}).get(k,0) for r in report['slots']) for k in TOKEN_KEYS},
        unknown_usage_requests=sum(r.get('unknown_usage_requests',0) for r in report['slots']),cost=None)
    save_json(directory/'summary.json',report)
    return report

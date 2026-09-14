"""Independent read-only oracle: raw PG channels + budget journal + HTTP/business truth."""
from datetime import datetime
import os
from pathlib import Path
import ormsgpack
from agentcheck_biz.checks import load_json,observe_database
from agentcheck_biz.persistence.store import IDENTITY_KEYS,digest,validate_manifest
from agentcheck_biz.persistence.verify import require
from agentcheck_biz.side_effects.store import binding
from agentcheck_biz.side_effects.verify import lines,api_observation
from .demo import scenarios
from .graph import validate
from .view import recovery_view,timestamp


def checkpoint_values(database,m,cp):
    raw=next(r for r in database['checkpoints'] if r['thread_id']==m['thread_id'] and r['checkpoint_id']==cp['checkpoint_id'])['checkpoint']
    values={}
    for key in (*IDENTITY_KEYS,'recovery_version','phase','messages','result','model_requests'):
        if key in raw['channel_values']:values[key]=raw['channel_values'][key]
        else:
            blob=next(r for r in database['checkpoint_blobs'] if r['thread_id']==m['thread_id'] and r['channel']==key and r['version']==raw['channel_versions'][key])
            require(blob['type']=='msgpack' and blob['blob'].startswith('\\x'),'Unsupported PG channel')
            values[key]=ormsgpack.unpackb(bytes.fromhex(blob['blob'][2:]))
    return values


def recheck(directory, *, scenario_names=None):
    directory=Path(directory).resolve()
    try:
        summary=load_json(directory/'summary.json')
        original=Path(summary['summary_path']).parent
        plans = scenarios()
        if scenario_names is not None:
            require(bool(scenario_names) and len(set(scenario_names)) == len(scenario_names), 'Empty or duplicate scenario selection')
            plans = [next(p for p in plans if p['name'] == name) for name in scenario_names]
        require(summary['status']=='PASS' and summary['planned']==summary['executed']==len(plans)==len(summary['scenarios']),'Incomplete continuity plan')
        require(summary['postgres_cleanup'] and all(r['exited'] for r in summary['postgres_cleanup']) and summary['gitea_cleanup']['exited'],'Service cleanup absent')
        kills,workers,model,tool=0,0,0,0
        for plan,record in zip(plans,summary['scenarios']):
            require(all(record[k]==v for k,v in plan.items()),'Changed D30 scenario')
            run=directory/Path(record['run_dir']).relative_to(original)
            m=load_json(run/'manifest.json')
            validate_manifest(m)
            op=m['side_effect']
            require(m['recovery']=={k:plan[k] for k in ('model_limit','tool_limit','wall_seconds')}|dict(version='continuity/1',client='offline-fixed/1'),'Changed frozen budget')
            captures={label:load_json(run/(label+'.json')) for label in ('at-gate','before-recovery','after-recovery','completed')}
            def selected(business):
                if op['kind']=='ticket':return [r for r in business['tickets'] if r['tenant_id']==op['scope'] and r['operation_id']==op['operation_id']]
                return [r for r in api_observation(run,business,op['repository']) if op['marker'] in r['body']]
            previous=[]
            deadline=None
            for label,snapshot in captures.items():
                db,cp,budget,ledger=snapshot['postgres'],snapshot['checkpoint'],snapshot['budget'],snapshot['ledger']
                require(cp and snapshot['checkpoint_sha256']==digest(cp),'Missing checkpoint')
                require(checkpoint_values(db,m,cp)==cp['values'],'Readable checkpoint differs from native PG channels')
                validate(cp['values'],m)
                expected_next={'new':['model'],'tool_pending':['tools'],'tool_verified':['final_model'],'completed':[]}[cp['values']['phase']]
                require(cp['next']==expected_next,'Checkpoint next step does not match the actual saved phase')
                require(next(r for r in db['ac_jobs'] if r['job_id']==m['job_id'])['manifest']==m,'Foreign persisted manifest')
                require(next(r for r in db['ac_operations'] if r['job_id']==m['job_id'])==ledger,'Ledger differs from PG')
                require(ledger['binding']==binding(m) and ledger['request_sha256']==digest(op['request']),'Changed operation binding')
                raw=next(r for r in db['ac_recovery_budgets'] if r['job_id']==m['job_id'])
                require(raw=={k:v for k,v in budget.items() if k not in {'events','database_now'}},'Budget differs from PG')
                events=sorted((r for r in db['ac_budget_events'] if r['job_id']==m['job_id']),key=lambda r:r['event_id'])
                require(events==budget['events'] and events[:len(previous)]==previous,'Budget audit reset or changed')
                previous=events
                require(deadline is None or deadline==raw['deadline'],'Deadline reset after restart')
                deadline=raw['deadline']
                seconds=(timestamp(deadline)-timestamp(raw['created_at'])).total_seconds()
                require(abs(seconds-plan['wall_seconds'])<.1,'Deadline is not the original database-clock allocation')
                for kind in ('model','tool'):
                    used=sum(e['kind']==kind for e in events)
                    require(used==raw[kind+'_calls']<=raw[kind+'_limit']==plan[kind+'_limit'],'Calls exceed or bypass persisted budget')
                require(len({e['call_id'] for e in events})==len(events),'Reused dispatch reservation')
                charges=[e for e in events if e['kind'] in {'model','tool'}]
                require(all(timestamp(e['created_at'])<timestamp(deadline) for e in charges),'Dispatch charged after deadline')
                if raw['stop_reason']:
                    stop=next(e for e in events if e['kind']==raw['stop_reason'])
                    require(all(e['event_id']<stop['event_id'] for e in charges),'Dispatch after durable stop')
                require(snapshot['view']==recovery_view(m,snapshot),'UI view disagrees with persisted evidence')
                actual=selected(snapshot['business'])
                if ledger['state']=='confirmed':require(actual==[ledger['result']],'Confirmed ledger differs from business truth')
                else:require(snapshot['view']['business_verdict']=='INCONCLUSIVE' and snapshot['view']['pending_operations'] and ledger['result'] is None,'Unknown shown as complete')
            at,after,final=(captures[k] for k in ('at-gate','after-recovery','completed'))
            require(record['view']==final['view'] and record['demonstration']==after['view'],'Summary changed recovery view')
            require(len(selected(final['business']))==plan['count']==record['final_count'],'Business count differs')
            require(selected(load_json(run/'final.json'))==selected(final['business']),'Final observation changed')
            if op['kind']=='ticket':
                require(load_json(run/'final.json')==final['business'],'Final Ticket observation changed')
                require(observe_database(run/'business.sqlite')==final['business'],'Final JSON differs from read-only SQLite')
                require([r for r in final['business']['tickets'] if r not in selected(final['business'])]==load_json(run/'initial.json')['tickets'],'Unrelated Ticket modified')
                require(load_json(run/'http-cleanup.json')['exited'],'Ticket process cleanup absent')
            else:
                initial=api_observation(run,load_json(run/'initial.json'),op['repository'])
                last=api_observation(run,load_json(run/'final.json'),op['repository'])
                require([r for r in last if r not in selected(final['business'])]==initial,'Unrelated Gitea Issue modified')
            barrier=load_json(run/plan['action']/'barrier.json')
            require(barrier['job_id']==m['job_id'] and barrier['window']==plan['window'],'Wrong fault gate')
            if record['killed']:
                killed=load_json(run/'crash/termination.json')
                require(killed==record['killed'] and killed['pid']==barrier['pid'] and killed['barrier_sha256']==digest(barrier),'Kill receipt changed')
                require(killed['exited'] and killed['exit_code']!=0 and killed['result_absent'] and not (run/'crash/result.json').exists(),'No actual abrupt kill')
                kills+=1
                workers+=1
            before_alive=load_json(run/'before-interruption-services.json')
            after_alive=load_json(run/'after-recovery-services.json')
            require(before_alive['service_pid']==after_alive['service_pid'] and before_alive['system_identifier']==after_alive['system_identifier'],'Business service or PG identity replaced')
            if plan['fault']=='storage':
                restart=load_json(run/'storage-restart.json')
                require(restart['cleanup'][-1]['exited'] and restart['after']['pid']!=before_alive['pg_pid'] and after_alive['pg_pid']==restart['after']['pid'],'No real PG outage and restart')
                require(record['storage_failures'] and all(r['result']['status']=='ERROR' for r in record['storage_failures']),'Unavailable storage did not fail closed')
            else:require(before_alive['pg_pid']==after_alive['pg_pid'],'Unexpected PG restart')
            db=final['postgres']
            claims=sorted((e for e in db['ac_lease_events'] if e['job_id']==m['job_id'] and e['kind'] in {'claimed','takeover'}),key=lambda r:r['generation'])
            require([c['generation'] for c in claims]==list(range(1,len(claims)+1)),'Reused or missing attempt generation')
            require(len({c['detail']['attempt_id'] for c in claims})==len(claims),'Attempt reused')
            for e in final['budget']['events']:
                require(e['attempt_id']==claims[e['generation']-1]['detail']['attempt_id'],'Foreign budget charge generation')
            seen=set()
            posts=0
            receipts=[]
            for worker in run.iterdir():
                if not worker.is_dir():continue
                dispatch=[e for e in lines(worker/'dispatch.jsonl') if e['event']=='dispatch']
                for e in dispatch:
                    charge=next(r for r in final['budget']['events'] if r['call_id']==e['call_id'])
                    require(charge['kind']==e['kind'] and charge['generation']==e['generation'] and e['call_id'] not in seen,'Dispatch without unique durable reservation')
                    require(e['deadline']==timestamp(deadline).timestamp(),'Transport got a fresh deadline')
                    seen.add(e['call_id'])
                http=[e for f,event in (('http-client.jsonl','http_request_started'),('http-observer.jsonl','http_request_started'),
                      ('gitea-execution.jsonl','api_request'),('gitea-observer.jsonl','api_request')) for e in lines(worker/f) if e['event']==event]
                require(len(http)==sum(e['kind']=='tool' for e in dispatch),'HTTP call bypassed budget ledger')
                posts+=sum(e['method']=='POST' for e in http)
                if (worker/'process.json').exists():
                    receipt=load_json(worker/'process.json')
                    result=load_json(worker/'result.json')
                    require(receipt['result']==result and receipt['exited'] and receipt['pid']==result['pid'] and result['model_requests']==0,'Invalid worker receipt')
                    require(receipt['exit_code']==dict(PASS=0,BUSY=2,INCONCLUSIVE=2,ERROR=3)[result['status']],'Worker exit differs from result')
                    if result.get('cached'):require(not dispatch and not http,'Cached resume redispatched')
                    receipts.append(receipt)
                    workers+=1
            require(seen=={e['call_id'] for e in final['budget']['events'] if e['kind'] in {'model','tool'}},'Missing pre-dispatch evidence')
            require(posts==plan['count'],'Same operation sent more than once')
            require(all(r in receipts for r in record['recovery']+record['later']+record['storage_failures']),'Missing subprocess evidence')
            require(any(r['result']['status']==plan['expected'] for r in record['recovery']),'Wrong recovery outcome')
            events=sorted((e for e in db['ac_operation_events'] if e['job_id']==m['job_id']),key=lambda e:e['revision'])
            require([e['revision'] for e in events]==list(range(final['ledger']['revision']+1)),'Missing operation audit')
            for e in events:
                if e['state']=='confirmed':
                    observation=e['evidence']
                    require(observation['complete'] is True and observation['items']==[final['ledger']['result']],'Confirmation lacks complete matching read')
                    writer=next(w for w in run.iterdir() if w.is_dir() and (w/'claim.json').exists() and load_json(w/'claim.json')['attempt_id']==e['attempt_id'])
                    if op['kind']=='gitea':api_observation(writer,observation['observation'],op['repository'])
                    else:require(any(r['event']=='http_response_received' and r['path']=='/tickets' and r['status_code']==200 for r in lines(writer/'http-observer.jsonl')),'No real confirming query')
            if plan['fault']=='concurrent':require(len(claims)==2 and len(record['recovery'])==3,'Concurrent resume acquired multiple executions')
            if plan['fault']=='abort':require(final['budget']['stop_reason']=='aborted' and final['view']['execution_state']=='aborted' and len(claims)==1,'Abort bypassed on resume')
            if plan['fault']=='stale':
                late=load_json(run/'after-late-return.json')
                require(late['checkpoint']==after['checkpoint'] and late['ledger']==after['ledger'] and late['budget']['events']==after['budget']['events'],'Old worker returned and overwrote state')
                require(load_json(run/'late-return.json')['result']['status']=='INCONCLUSIVE','Old worker accepted')
            if plan['fault']=='unavailable':
                require(lines(run/'outage-events.jsonl') and all(e['status_code']==503 for e in lines(run/'outage-events.jsonl')),'Missing actual read failure')
                require(after['view']['business_verdict']=='INCONCLUSIVE' and final['view']['business_verdict']=='PASS','Insufficient evidence fabricated success or failed to resume')
            if plan['name']=='saved-tool-call':require(at['budget']['model_calls']==1 and final['budget']['model_calls']==2 and len(at['checkpoint']['values']['messages'])==1,'Saved model selection was repeated')
            if plan['name']=='model-reservation-lost':require(final['budget']['model_calls']==1 and final['budget']['tool_calls']==0,'Lost model reservation refunded')
            if plan['name']=='tool-budget-exhausted':require(final['budget']['tool_calls']==2 and final['view']['pending_operations'],'Tool limit bypassed')
            if plan['name']=='final-model-budget':require(final['view']['business_verdict']=='PASS' and final['view']['execution_state']=='budget_stopped','Execution and business verdict conflated')
            if plan['fault']=='deadline':require(final['view']['stop_reason']=='deadline_exhausted' and final['budget']['tool_calls']==0,'Deadline reset')
            if final['view']['execution_state']=='completed':
                require(final['ledger']['state']=='confirmed' and not final['checkpoint']['next'] and len(final['checkpoint']['values']['messages'])==3,'Incomplete Agent displayed completed')
            model+=final['budget']['model_calls']
            tool+=final['budget']['tool_calls']
        return dict(status='PASS',experiments=len(plans),forced_agent_kills=kills,worker_processes=workers,
                    offline_model_calls=model,tool_calls=tool,model_requests=0,verifier_pid=os.getpid())
    except Exception as exc:
        return dict(status='ERROR',reason=type(exc).__name__+': '+str(exc),scenario=locals().get('plan',{}).get('name'),verifier_pid=os.getpid())

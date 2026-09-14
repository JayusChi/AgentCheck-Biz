"""Read-only evidence oracle: native PG channels, HTTP/API records and SQLite truth."""
import json
from pathlib import Path
import sqlite3
import ormsgpack
from agentcheck_biz.checks import load_json, observe_database
from agentcheck_biz.observers.gitea import normalize_issue, normalize_repository
from agentcheck_biz.persistence.store import digest, IDENTITY_KEYS, validate_manifest
from agentcheck_biz.persistence.verify import require
from agentcheck_biz.scheduling.verify import timestamp
from .store import binding
from .demo import scenarios


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line.strip()] if path.exists() else []


def checkpoint_values(database, manifest, cp):
    row = next(r for r in database['checkpoints'] if r['thread_id']==manifest['thread_id'] and r['checkpoint_id']==cp['checkpoint_id'])
    channels, versions = row['checkpoint']['channel_values'], row['checkpoint']['channel_versions']
    result = {}
    for key in (*IDENTITY_KEYS,'phase','result','model_requests'):
        if key in channels:
            result[key] = channels[key]
        else:
            blob = next(b for b in database['checkpoint_blobs'] if b['thread_id']==manifest['thread_id']
                and b['channel']==key and b['version']==versions[key])
            require(blob['type']=='msgpack' and blob['blob'].startswith('\\x'),'Unknown checkpoint channel encoding')
            result[key] = ormsgpack.unpackb(bytes.fromhex(blob['blob'][2:]))
    return result


def api_observation(root, observation, repository):
    require(observation['complete'] is True,'Incomplete Gitea business observation')
    data = observation['data']
    def response(relative):
        path = (root/relative).resolve()
        require(path.is_relative_to(root.resolve()),'Foreign API evidence path')
        value = load_json(path)
        require(value['method']=='GET' and value['role']=='observer' and value['status_code']==200,'Non-readonly API evidence')
        return value
    require(normalize_repository(response(data['repository_evidence'])['body'])==repository==data['repository'],'Repository mismatch')
    rows = []
    pagination = data['pagination']
    require(pagination['complete'] is True and pagination['state']=='all' and pagination['type']=='issues','Incomplete API scope')
    for index, page in enumerate(pagination['pages'],1):
        value = response(page['evidence'])
        require(page['page']==index and value['params']==dict(state='all', type='issues',limit=pagination['page_size'],page=index),'Pagination scope changed')
        require(int(value['headers']['x-total-count'])==pagination['total_count'],'API total changed')
        batch = [normalize_issue(item,repository) for item in value['body']]
        require(page['ids']==[r['id'] for r in batch],'Page IDs differ')
        rows.extend(batch)
    require(len(rows)==pagination['total_count']==len({r['id'] for r in rows}),'Incomplete or duplicate API pages')
    require(sorted(rows,key=lambda r:r['number'])==data['issues'],'Saved Issue list differs from API')
    details = [normalize_issue(response(p)['body'],repository) for p in data['detail_evidence']]
    require(sorted(details,key=lambda r:r['number'])==data['issues'],'Missing independent Issue detail')
    return data['issues']


def recheck(directory):
    directory = Path(directory).resolve()
    try:
        summary = load_json(directory/'summary.json')
        original = Path(summary['summary_path']).parent
        require(summary['status']=='PASS' and summary['planned']==summary['executed']==len(scenarios())
                and len(summary['scenarios'])==len(scenarios()),'Incomplete D29 plan')
        require(summary['postgres_cleanup'] and all(r['exited'] for r in summary['postgres_cleanup'])
                and summary['gitea_cleanup']['exited'],'Owned service cleanup missing')
        pids, outcomes = set(), []
        for plan, record in zip(scenarios(),summary['scenarios']):
            require(all(record[k]==v for k,v in plan.items()),'Changed scenario plan')
            run = directory/Path(record['run_dir']).relative_to(original)
            manifest = load_json(run/'manifest.json')
            validate_manifest(manifest)
            op = manifest['side_effect']
            require(op['kind']==plan['kind'] and op['operation_id']==manifest['operation_id'],'Operation identity changed')
            captures = {name:load_json(run/(name+'.json')) for name in ('at-gate','after-kill','before-recovery','after-recovery','completed')}
            def selected(business):
                rows = business['tickets'] if op['kind']=='ticket' else api_observation(run,business,op['repository'])
                return [r for r in rows if (r['tenant_id']==op['scope'] and r['operation_id']==op['operation_id'])] if op['kind']=='ticket' else [r for r in rows if op['marker'] in r['body']]
            for saved in captures.values():
                database, cp, ledger = saved['postgres'], saved['checkpoint'], saved['ledger']
                require(cp and digest(cp)==saved['checkpoint_sha256'],'Missing or changed checkpoint digest')
                require(checkpoint_values(database,manifest,cp)==cp['values'],'Checkpoint JSON differs from native PG channels')
                require(all(cp['values'][k]==manifest[k] for k in IDENTITY_KEYS) and cp['values']['model_requests']==0,'Checkpoint binding changed')
                require(next(r for r in database['ac_jobs'] if r['job_id']==manifest['job_id'])['manifest']==manifest,'Database manifest differs')
                require(next(r for r in database['ac_operations'] if r['job_id']==manifest['job_id'])==ledger,'Ledger differs from PG export')
                require(ledger['binding']==binding(manifest) and ledger['request_sha256']==digest(op['request']),'Ledger request binding differs')
                selected(saved['business'])
            at, after, final = (captures[k] for k in ('at-gate','after-recovery','completed'))
            require(at['checkpoint']==captures['after-kill']['checkpoint'] and at['ledger']==captures['after-kill']['ledger'],'Agent kill changed durable state')
            require(selected(at['business'])==selected(captures['after-kill']['business']),'Agent kill changed business records')
            require(at['ledger']['state']==('confirmed' if plan['window']=='after_checkpoint' else 'prepared' if plan['window']=='before_call' else 'sent_unknown'),'Wrong ledger crash window')
            require(at['checkpoint']['next']==(['finish'] if plan['window']=='after_checkpoint' else ['apply']),'Wrong checkpoint crash window')
            require(len(selected(at['business']))==(0 if plan['window'] in {'before_call','sent_unknown'} else 1),'Wrong actual commit window')
            barrier, killed = load_json(run/'crash/barrier.json'), load_json(run/'crash/termination.json')
            require(killed==record['killed'] and killed['pid']==barrier['pid'] and digest(barrier)==killed['barrier_sha256'],'Kill receipt mismatch')
            require(killed['exited'] and killed['exit_code']!=0 and killed['termination']=='owned_Popen.kill'
                    and killed['result_absent'] and not (run/'crash/result.json').exists(),'Not a real abrupt Agent kill')
            require(barrier['window']==plan['window'] and all(barrier[k]==manifest[k] for k in ('job_id','thread_id','operation_id','environment_id')),'Barrier belongs to another execution')
            require(killed['pid'] not in pids,'PID reused across evidence')
            pids.add(killed['pid'])
            alive = [load_json(run/(name+'-services.json')) for name in ('before-kill','after-kill','after-recovery')]
            require(all(all(a[k]==alive[0][k] for k in alive[0] if k!='at') for a in alive),'Business service or PG restarted')
            require(alive[0]['pg_pid']==summary['postgres']['pid'] and alive[0]['system_identifier']==summary['postgres']['system_identifier'],'Foreign PG identity')
            require(timestamp(alive[0]['at'])<=timestamp(killed['killed_at'])<=timestamp(killed['observed_exit_at'])<=timestamp(alive[1]['at'])<=timestamp(alive[2]['at']),'Kill/service ordering invalid')
            database = final['postgres']
            events = sorted((r for r in database['ac_operation_events'] if r['job_id']==manifest['job_id']),key=lambda r:r['revision'])
            require([r['revision'] for r in events]==list(range(final['ledger']['revision']+1)),'Missing operation events')
            require(events[0]['state']=='prepared' and events[-1]['state']==final['ledger']['state'],'Wrong ledger timeline')
            allowed = {'prepared':{'sent_unknown'},'sent_unknown':{'sent_unknown','confirmed','conflict'},'confirmed':set(),'conflict':set()}
            require(all(b['state'] in allowed[a['state']] for a,b in zip(events,events[1:])),'Illegal ledger transition')
            lease_events = sorted((r for r in database['ac_lease_events'] if r['job_id']==manifest['job_id']),key=lambda r:r['event_id'])
            claims = [r for r in lease_events if r['kind'] in {'claimed','takeover'}]
            require([r['generation'] for r in claims]==list(range(1,len(claims)+1)) and claims[0]['detail']['pid']==killed['pid'],'Missing or reused lease generation')
            require(any(r['kind']=='expired' and r['generation']==1 for r in lease_events),'No actual lease expiry')
            require(len({r['detail']['attempt_id'] for r in claims})==len(claims),'Attempt reused')
            for event in events:
                require(event['attempt_id']==claims[event['generation']-1]['detail']['attempt_id'],'Ledger written by wrong generation')
            receipts = []
            for label in sorted(run.iterdir()):
                if label.is_dir() and (label/'process.json').exists():
                    receipt = load_json(label/'process.json')
                    result = load_json(label/'result.json')
                    require(receipt['result']==result and receipt['pid']==result['pid'] and receipt['exited'],'Missing real recovery result')
                    require(result['model_requests']==0 and receipt['exit_code']==dict(PASS=0,BUSY=2,INCONCLUSIVE=2,ERROR=3)[result['status']],'Recovery exit code mismatch')
                    if result.get('lease'):
                        require(result['lease']==load_json(label/'claim.json') and result['lease']['generation']>=2,'Bad recovery lease')
                    require(receipt['pid']!=killed['controller_pid'] and receipt['pid'] not in pids,'Recovery PID reused')
                    pids.add(receipt['pid'])
                    receipts.append(receipt)
                    if result.get('cached'):
                        require(not (label/'http-client.jsonl').exists() and not (label/'gitea-execution.jsonl').exists(),'Confirmed result repeated HTTP')
            require(all(r in receipts for r in record['recovery']+record['later']),'Summary changed worker records')
            require(any(r['result']['status']==plan['expected'] for r in record['recovery']),'Recovery missed expected status')
            for event in events:
                if event['state'] != 'confirmed':
                    continue
                observation = event['evidence']
                require(observation['complete'] is True and observation['items']==[final['ledger']['result']],
                        'Ledger confirmation lacks a matching complete query')
                worker = next(p for p in run.iterdir() if p.is_dir() and (p/'claim.json').exists()
                              and load_json(p/'claim.json')['attempt_id']==event['attempt_id'])
                if op['kind']=='gitea':
                    observed = api_observation(worker, observation['observation'], op['repository'])
                    require([r for r in observed if op['marker'] in r['body']]==observation['items'],
                            'Ledger confirmation differs from independent Gitea API')
                else:
                    require(any(e['event']=='http_response_received' and e['method']=='GET' and e['path']=='/tickets'
                                and e['status_code']==200 for e in lines(worker/'http-observer.jsonl')),
                            'Ledger confirmation lacks a real Ticket query')
            actual = selected(load_json(run/'final.json'))
            require(actual==selected(final['business']) and len(actual)==record['final_count']==plan['count'],'Business result differs')
            require(record['final_ledger']==final['ledger']['state'],'Summary changed ledger state')
            job = next(r for r in database['ac_jobs'] if r['job_id']==manifest['job_id'])
            lease = next(r for r in database['ac_leases'] if r['job_id']==manifest['job_id'])
            require(lease['owner'] is None,'Recovery lease not released')
            if final['ledger']['state']=='confirmed':
                require(actual==[final['ledger']['result']] and job['state']=='finished' and not final['checkpoint']['next'],'Unconfirmed completion')
                require(final['checkpoint']['values']['result']==actual[0] and job['checkpoint_id']==final['checkpoint']['checkpoint_id'],'Completion checkpoint mismatch')
            else:
                require(job['state']=='waiting_verification' and final['ledger']['result'] is None,'Unknown counted as success')
            if op['kind']=='ticket':
                require(op['target']['app_version']=='fixed' and alive[0]['service_pid']==op['target']['pid'],'Unverified Ticket contract')
                require(observe_database(run/'business.sqlite')==load_json(run/'final.json'),'Edited final JSON differs from SQLite')
                initial = load_json(run/'initial.json')['tickets']
                require([r for r in observe_database(run/'business.sqlite')['tickets'] if r not in actual]==initial,'Unrelated Ticket changed')
                with sqlite3.connect((run/'business.sqlite').as_uri()+'?mode=ro',uri=True) as conn:
                    conn.row_factory=sqlite3.Row
                    mappings=[dict(r) for r in conn.execute('SELECT * FROM idempotency_keys')]
                require(mappings==load_json(run/'idempotency-mappings.json'),'Idempotency mapping evidence changed')
                mapped=[r for r in mappings if r['tenant_id']==op['scope'] and r['operation_id']==op['operation_id']]
                require(len(mapped)==1 and mapped[0]['ticket_id']==actual[0]['ticket_id'] and mapped[0]['request_hash']==digest(op['request']),'Atomic key mapping differs')
                served=lines(run/'http-service-events.jsonl')
                require(sum(r['event']=='ticket_created' and r.get('ticket')==actual[0] for r in served)==1,'Missing single actual Ticket creation')
                worker_posts=[]
                for label in run.iterdir():
                    if label.is_dir():
                        worker_posts += [(label.name,e) for e in lines(label/'http-client.jsonl') if e['event']=='http_request_started' and e['method']=='POST']
                expected_posts=2 if plan['strategy']=='replay' else 1
                require(len(worker_posts)==expected_posts,'Unexpected recovery POST count')
                if plan['window']=='after_checkpoint':
                    require(all(label=='crash' for label,e in worker_posts),'Saved checkpoint repeated write')
                if plan['name']=='ticket-query-1':
                    require(load_json(run/'content-conflict.json')['http_status']==409 and any(e.get('request_id')=='different-content' and e.get('status_code')==409 for e in served),'Missing real same-key conflict')
                require(load_json(run/'http-cleanup.json')['exited'],'Ticket process cleanup missing')
            else:
                require(alive[0]['service_pid']==summary['gitea']['pid'],'Foreign Gitea process')
                initial=api_observation(run,load_json(run/'initial.json'),op['repository'])
                final_rows=api_observation(run,load_json(run/'final.json'),op['repository'])
                require([r for r in final_rows if r not in actual]==initial,'Unrelated Gitea Issue changed')
                for label in run.iterdir():
                    if label.is_dir() and label.name!='crash':
                        require(all(e['method']=='GET' for e in lines(label/'gitea-execution.jsonl') if e['event']=='api_request'),'Gitea recovery repeated create')
                if plan['fault']=='duplicate':
                    require(load_json(run/'duplicate-injection.json')['status_code']==201 and final['ledger']['state']=='conflict','Duplicate not retained as conflict')
            if plan['fault']=='unavailable':
                require(lines(run/'outage-events.jsonl') and all(e['status_code']==503 for e in lines(run/'outage-events.jsonl')),'No actual HTTP read outage')
                require(after['ledger']['state']=='sent_unknown' and record['later'][0]['result']['status']=='PASS','Outage reset unknown or did not recover')
                require(load_json(run/'outage-cleanup.json')['exited'],'Outage process cleanup missing')
            outcomes.append(plan['expected'])
        return dict(status='PASS', experiments=len(outcomes), forced_agent_kills=len(outcomes), worker_processes=len(pids),
            ticket_fixed_replays=3, concurrent_recovery_cases=2, read_outage_cases=2,
            expected_outcomes={s:outcomes.count(s) for s in ('PASS','INCONCLUSIVE')},model_requests=0)
    except Exception as exc:
        return dict(status='ERROR',reason=type(exc).__name__+': '+str(exc))

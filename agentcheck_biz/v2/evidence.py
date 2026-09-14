"""Reconstruct summaries from raw artifacts; never infer PASS from child exit alone."""
from pathlib import Path
from agentcheck_biz.checks import load_json
from agentcheck_biz.persistence.store import digest
from agentcheck_biz.network_acceptance.verify import hashes, inspect_run, fresh_identity
from agentcheck_biz.continuity.verify import recheck as continuity_check
from agentcheck_biz.continuity.view import recovery_view
from . import EVIDENCE_VERSION


def within(root, relative):
    root=Path(root).resolve()
    path=(root/relative).resolve()
    if path==root or root not in path.parents:raise ValueError('Evidence path escapes slot')
    return path


def inspect(directory, slot):
    directory=Path(directory)
    outcome=load_json(directory/'outcome.json')
    if outcome['slot_id']!=slot['slot_id'] or outcome['profile']!=slot['profile']:
        raise ValueError('Foreign slot outcome')
    run=within(directory,outcome['run'])
    if slot['engine']=='network':
        from agentcheck_biz.network_acceptance.profiles import profiles
        profile=next(p for p in profiles() if 'network:'+p['id']==slot['profile'])
        observed=inspect_run(run,profile['kind'])
        identity=fresh_identity(run,profile['kind'])
        name={'proxy':'run.json','recovery':'recovery-run.json','crash':'crash-run.json'}[profile['kind']]
        raw=load_json(run/name)
        case=raw.get('case') or load_json(run/'case.json')
        if case!=slot['case']:raise ValueError('Executed case differs from manifest')
        # Services and protocol are also checked by their existing raw oracles.
        actual_version=(load_json(next((run/'instances').glob('*/ready.json')))['app_version']
                        if profile['kind']=='crash' else raw.get('app_version'))
        if actual_version!=slot['object']['version']:
            raise ValueError('Actual target version differs from manifest')
        if profile['kind']=='proxy' and ('MCP/'+str(raw.get('mcp_protocol_version'))+'/stdio+HTTP/1.1'!=slot['adapter']['protocol']):
            raise ValueError('Negotiated protocol differs from manifest')
        observed['observation_status']='failed' if observed.get('error') else 'complete'
        return dict(observed=observed,identity=identity,raw_path=str(run), oracle_ok=observed['evidence_status'] in {'PASS','INCONCLUSIVE'})
    summary=load_json(run/'summary.json')
    checked=continuity_check(run,scenario_names=[slot['profile'].split(':',1)[1]])
    if checked['status']!='PASS':raise ValueError('Continuity raw oracle failed: '+str(checked))
    record=summary['scenarios'][0]
    child=within(run,Path(record['run_dir']).relative_to(Path(summary['summary_path']).parent))
    m=load_json(child/'manifest.json')
    after=load_json(child/'after-recovery.json')
    final=load_json(child/'completed.json')
    view=recovery_view(m,after)
    if {r['job_id'] for r in final['postgres']['ac_jobs']}!={m['job_id']}:
        raise ValueError('Foreign job shares slot storage')
    identity={k:m[k] for k in ('job_id','experiment_id','environment_id','thread_id','run_id')}
    identity.update(postgres_system=summary['postgres']['system_identifier'],gitea_instance=summary['gitea']['instance_id'])
    initial=load_json(child/'initial.json')
    if slot['object']['kind']=='ticket' and initial['tickets']!=slot['initial_state']:
        raise ValueError('Initial state differs from manifest')
    observed=dict(agent_status=next(r['result']['status'] for r in record['recovery'] if r['result']['status']!='BUSY'),
        business_status=view['business_verdict'],evidence_status='PASS',
        coverage='covered',resource_count=record['final_count'],
        observation_status='failed' if record['fault']=='unavailable' else 'complete',
        final_business_status=record['view']['business_verdict'], details=checked)
    return dict(observed=observed,identity=identity,raw_path=str(child),oracle_ok=True)


def aggregate(slots):
    groups={}
    def add(key,slot):groups.setdefault(key,[]).append(slot['slot_id'])
    for slot in slots:
        add('planned',slot)
        add('state:'+slot['state'],slot)
        add('profile:'+slot['profile'],slot)
        observed=slot.get('inspection',{}).get('observed',{})
        for field in ('business_status','coverage','observation_status'):
            add(field+':'+str(observed.get(field,'MISSING')),slot)
        add('expectation:'+str(slot.get('matched',False)).lower(),slot)
    return {key:dict(count=len(ids),slot_ids=ids) for key,ids in sorted(groups.items())}


def check(directory, manifest):
    """Caller must install the read-only guard in an independent process."""
    directory=Path(directory).resolve()
    report=load_json(directory/'summary.json')
    plan=load_json(directory/'plan.json')
    if report['evidence_version']!=EVIDENCE_VERSION or plan['evidence_version']!=EVIDENCE_VERSION:
        raise ValueError('Unsupported evidence version')
    if digest(plan['manifest'])!=digest(manifest) or report['manifest_sha256']!=digest(manifest):
        raise ValueError('Batch does not match supplied fixed manifest')
    expected_ids=[s['slot_id'] for s in manifest['slots']]
    if [s['slot_id'] for s in report['slots']]!=expected_ids:
        raise ValueError('Missing, reordered or duplicate slot')
    receipts=[]
    seen={}
    for spec,slot in zip(manifest['slots'],report['slots']):
        if slot['profile']!=spec['profile']:raise ValueError('Slot profile changed')
        state_path=within(directory,'slots/'+spec['slot_id']+'/slot.json')
        state=load_json(state_path)
        # A dead coordinator may have an older summary. Keep the entire frozen
        # denominator, but never certify a partial or inconsistent snapshot.
        if slot!=state:raise ValueError('Summary differs from durable slot state')
        if slot['state'] not in {'not_started','started','completed','error'}:raise ValueError('Unknown slot state')
        if ([e['sequence'] for e in slot['events']]!=list(range(len(slot['events'])))
                or slot['events'][0]['state']!='not_started' or slot['events'][-1]['state']!=slot['state']):
            raise ValueError('Slot transition history is incomplete')
        if slot['state']!='completed':
            if slot.get('evidence_sha256') is not None and hashes(state_path.parent/'payload')!=slot['evidence_sha256']:
                raise ValueError('Partial evidence hash changed')
            receipts.append(dict(slot_id=spec['slot_id'],state=slot['state'],matched=False))
            continue
        payload=state_path.parent/'payload'
        if not slot['process']['exited'] or slot['process']['exit_code']!=0:
            raise ValueError('Completed slot has no successful owned process receipt')
        if hashes(payload)!=slot['evidence_sha256']:raise ValueError('Evidence hash changed')
        identity=load_json(payload/'worker-identity.json')
        if (identity['pid']!=slot['process']['pid'] or identity['slot_id']!=spec['slot_id']
                or identity['manifest_sha256']!=digest(manifest) or identity['owned_descendant_job'] is not True):
            raise ValueError('Worker belongs to another slot or manifest')
        actual=inspect(payload,spec)
        # verifier PID is intentionally different between producer and check.
        def stable(data):
            data=__import__('copy').deepcopy(data)
            data.get('observed',{}).get('details',{}).pop('verifier_pid',None)
            # Archives can be copied; original runtime identities stay bound,
            # while the inspection's local filesystem address may relocate.
            data['raw_path']=Path(data['raw_path']).name
            if 'database_directory' in data['identity']:
                data['identity']['database_directory']=Path(data['identity']['database_directory']).name
            return data
        if stable(actual)!=stable(slot['inspection']):raise ValueError('Summary disagrees with raw evidence')
        for key,value in actual['identity'].items():
            if value in seen.setdefault(key,set()):raise ValueError('Shared cross-slot identity: '+key)
            seen[key].add(value)
        matched=actual['oracle_ok'] and all(actual['observed'].get(k)==v for k,v in spec['expected'].items())
        if matched!=slot['matched']:raise ValueError('Changed expectation result')
        receipts.append(dict(slot_id=spec['slot_id'],state=slot['state'],matched=matched,
                             identity=actual['identity'],raw_path=actual['raw_path']))
    if report['aggregate']!=aggregate(report['slots']):raise ValueError('Aggregate count or slot links changed')
    complete=bool(receipts) and all(r['state']=='completed' for r in receipts)
    status='PASS' if complete and all(r['matched'] for r in receipts) else 'FAIL' if complete else 'INCONCLUSIVE'
    if complete and report['status']!=status:raise ValueError('Batch status disagrees with completed slots')
    if report['status']=='PASS' and status!='PASS':raise ValueError('Partial batch falsely labeled PASS')
    return dict(status=status,planned=len(receipts),executed=sum(r['state']=='completed' for r in receipts),
                slots=receipts,aggregate=report['aggregate'],read_only=True,model_requests=0)

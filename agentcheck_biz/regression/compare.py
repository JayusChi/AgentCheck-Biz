"""Frozen inventory comparison and explicit release policies."""
from copy import deepcopy
from pathlib import Path

from agentcheck_biz.checks import load_json
from agentcheck_biz.persistence.store import digest
from agentcheck_biz.v2.manifest import read, validate
from agentcheck_biz.v2.evidence import check
from . import REPORT_VERSION, POLICY_VERSION
from .facts import extract

STATES = ('PASS','FAIL','ERROR','INCONCLUSIVE')


def worst(states):
    states = list(states)
    return next((s for s in ('ERROR','FAIL','INCONCLUSIVE') if s in states), 'PASS' if states else 'INCONCLUSIVE')


def policy(value=None):
    value = deepcopy(value) if value is not None else dict(schema_version=POLICY_VERSION, case_exceptions=[])
    if set(value) != {'schema_version','case_exceptions'} or value['schema_version'] != POLICY_VERSION:
        raise ValueError('Unsupported release policy')
    exceptions = value['case_exceptions']
    if not isinstance(exceptions,list) or len(exceptions)>1:
        raise ValueError('Only the explicit Gitea pending policy is registered')
    for rule in exceptions:
        if (set(rule) != {'profile','case_id','rule','reason'}
            or rule['profile'] != 'continuity:gitea-unknown-empty'
            or rule['case_id'] != 'G01_create'
            or rule['rule'] != 'pending-without-replay/1'
            or not isinstance(rule['reason'],str) or not 1 <= len(rule['reason'].strip()) <= 1000):
            raise ValueError('Exception must name the registered case and conservative rule with a reason')
    return value


def load_batch(manifest_path, directory):
    manifest = read(manifest_path)
    validate(manifest, environment=False, current_source=False)
    directory = Path(directory).resolve()
    result = dict(manifest=manifest, directory=str(directory), slots={}, error=None)
    try:
        verified = check(directory,manifest)
        summary = load_json(directory/'summary.json')
        for spec, receipt, slot in zip(manifest['slots'],verified['slots'],summary['slots']):
            record = dict(state=receipt['state'], harness_matched=receipt['matched'])
            if receipt['state']=='completed':
                observed = deepcopy(slot['inspection']['observed'])
                record.update(observed={k:v for k,v in observed.items() if k!='details'},
                    facts=extract(receipt['raw_path'],spec,observed), identity=receipt['identity'])
            result['slots'][spec['slot_id']] = record
        result['harness_status'] = verified['status']
    except Exception as error:
        result['error'] = type(error).__name__+': '+str(error)
        result['slots'] = {s['slot_id']:dict(state='invalid',error=result['error']) for s in manifest['slots']}
    return result


def contract(spec, record, release_policy):
    """This gate deliberately never reads spec.expected or harness_matched."""
    if record is None:return dict(status='INCONCLUSIVE',reason='Slot missing',exception=None)
    if record['state'] in {'invalid','error'}:
        return dict(status='ERROR',reason=record.get('error','Execution failed'),exception=None)
    if record['state']!='completed':
        return dict(status='INCONCLUSIVE',reason='Slot '+record['state'],exception=None)
    observed, facts = record['observed'],record['facts']
    raw = observed['business_status']
    if raw not in STATES or observed['evidence_status'] not in STATES:
        return dict(status='ERROR',reason='Unknown original verdict',exception=None)
    if observed['evidence_status']=='ERROR':
        return dict(status='ERROR',reason='Raw evidence oracle failed',exception=None)
    failed = [i['id'] for i in facts['invariants'] if i['status']=='FAIL']
    if failed or raw=='FAIL':
        return dict(status='FAIL',reason='Business contract failed: '+(', '.join(failed) or 'raw oracle'),exception=None)
    if raw=='ERROR':return dict(status='ERROR',reason='Business execution ERROR',exception=None)
    if (observed['evidence_status']!='PASS' or observed['coverage'] not in {'covered','transparent'}
        or observed['observation_status']!='complete'
        or any(i['status']=='INCONCLUSIVE' for i in facts['invariants'])):
        return dict(status='INCONCLUSIVE',reason='Incomplete observation or fault coverage',exception=None)
    if raw=='PASS':return dict(status='PASS',reason='Candidate satisfies the business contract',exception=None)
    for rule in release_policy['case_exceptions']:
        if spec['profile']==rule['profile'] and spec['case']['case_id']==rule['case_id'] and facts['conservative_pending']:
            return dict(status='PASS',reason='Explicit case policy accepts pending without replay',exception=rule)
    return dict(status='INCONCLUSIVE',reason='Unconfirmed business result; no applicable case exception',exception=None)


def conditions(manifest, spec, record):
    facts = (record or {}).get('facts',{})
    return dict(case=spec['case'],initial_state=spec['initial_state'],fault=spec['fault'],budget=spec['budget'],
        recovery=spec['recovery'],adapter=spec['adapter'],engine=spec['engine'],object_kind=spec['object']['kind'],
        execution=manifest['execution'],prompt=facts.get('prompt'),observation_source=facts.get('observer'),
        runtime=facts.get('runtime'))


def align(left, right, left_spec, right_spec, a, b):
    if left_spec is None or right_spec is None:
        return dict(comparable=False,differences=[dict(field='slot_inventory',baseline=bool(left_spec),candidate=bool(right_spec))],
                    attribution='blocked',attribution_reason='Missing or additional slot',changes=[])
    x,y = conditions(left,left_spec,a),conditions(right,right_spec,b)
    differences = [dict(field=k,baseline=x[k],candidate=y[k]) for k in x if x[k]!=y[k]]
    missing = [k for k in ('prompt','observation_source','runtime') if x[k] is None or y[k] is None]
    changes = []
    for name, av,bv in [('object.version',left_spec['object']['version'],right_spec['object']['version']),
                       ('source_sha256',left['source_sha256'],right['source_sha256'])]:
        if av!=bv:changes.append(dict(field=name,baseline=av,candidate=bv))
    comparable = not differences and not missing
    single = comparable and [c['field'] for c in changes]==['object.version']
    attribution = 'service_version_only' if single else 'repeat' if comparable and not changes else 'blocked'
    reason = ('Only the registered service version changed; this is a paired observation, not a statistical causal estimate'
        if single else 'Identical controlled inputs; no change to attribute' if attribution=='repeat'
        else 'Control differences, missing provenance, or source changes prevent single-factor attribution')
    return dict(comparable=comparable,differences=differences,missing_conditions=missing,
                changes=changes,attribution=attribution,attribution_reason=reason)


def snapshot(record, gate):
    if record is None:return None
    return dict(state=record['state'],observed=record.get('observed'),identity=record.get('identity'),
        harness_matched=record.get('harness_matched'),contract=gate,facts=record.get('facts'),error=record.get('error'))


def compare_batches(baseline, candidate, *, suite='release', release_policy=None):
    if suite not in {'release','harness'}:raise ValueError('Unknown suite')
    effective = policy(release_policy)
    if suite=='release' and baseline is None:raise ValueError('Release comparison requires a baseline')
    if suite=='harness' and baseline is not None:raise ValueError('Harness self-test does not compare a baseline')
    bm = baseline['manifest'] if baseline else None
    cm = candidate['manifest']
    cs = {s['slot_id']:s for s in cm['slots']}
    bs = {s['slot_id']:s for s in bm['slots']} if bm else {}
    if len(cs)!=len(cm['slots']) or (bm and len(bs)!=len(bm['slots'])):raise ValueError('Duplicate slot ID')
    ids = list(bs)+[key for key in cs if key not in bs]
    rows = []
    controls = []
    if not ids:controls.append(dict(status='INCONCLUSIVE',reason='Zero planned slots'))
    if bm and list(bs)!=list(cs):controls.append(dict(status='INCONCLUSIVE',reason='Slot inventory or order changed'))
    if baseline and baseline['directory']==candidate['directory']:
        controls.append(dict(status='INCONCLUSIVE',reason='Baseline and candidate must be independent batches'))
    reused = {}
    if baseline:
        identities = {}
        for record in baseline['slots'].values():
            for field,value in record.get('identity',{}).items():
                identities.setdefault(field,set()).add(value)
        for key,record in candidate['slots'].items():
            repeated = [field for field,value in record.get('identity',{}).items()
                        if value in identities.get(field,set())]
            if repeated:reused[key]=repeated
        if reused:controls.append(dict(status='INCONCLUSIVE',reason='Candidate reuses baseline runtime identities',slots=reused))
    for label,batch in [('baseline',baseline),('candidate',candidate)]:
        if batch and batch.get('error'):controls.append(dict(status='ERROR',reason=label+': '+batch['error']))
    for key in ids:
        a = baseline['slots'].get(key) if baseline else None
        b = candidate['slots'].get(key)
        ag = contract(bs.get(key),a,effective) if bm else None
        bg = contract(cs.get(key),b,effective)
        alignment = align(bm,cm,bs.get(key),cs.get(key),a,b) if bm else None
        if alignment and key in reused:
            alignment.update(comparable=False,attribution='blocked',reused_identities=reused[key],
                             attribution_reason='Candidate reuses baseline runtime identities')
        if suite=='release':
            statuses = [bg['status']]
            # A baseline FAIL is valid evidence of an improvement. Missing/error
            # baselines cannot establish a regression comparison.
            if ag['status'] in {'ERROR','INCONCLUSIVE'}:statuses.append(ag['status'])
            if not alignment['comparable']:statuses.append('INCONCLUSIVE')
            status = worst(statuses)
        else:
            status = ('ERROR' if not b or b['state'] in {'invalid','error'} else
                'INCONCLUSIVE' if b['state']!='completed' else 'PASS' if b['harness_matched'] else 'FAIL')
        deltas = []
        if a and b and a.get('facts') and b.get('facts'):
            old = {i['id']:i for i in a['facts']['invariants']}
            for item in b['facts']['invariants']:
                prior = old[item['id']]
                deltas.append(dict(id=item['id'],baseline=prior,candidate=item,
                    new_violation=(prior['status']=='PASS' and item['status']=='FAIL') if alignment['comparable'] else None))
        reason = bg['reason'] if suite=='release' else 'Expected harness verdict matched' if status=='PASS' else 'Harness expectation incomplete or mismatched'
        if alignment and not alignment['comparable']:reason+='; comparison conditions are not aligned'
        rows.append(dict(slot_id=key,case_id=(cs.get(key) or bs[key])['case']['case_id'],
            profile=(cs.get(key) or bs[key])['profile'],status=status,reason=reason,
            baseline=snapshot(a,ag),candidate=snapshot(b,bg),alignment=alignment,invariant_deltas=deltas,
            harness_expected=cs[key]['expected'] if key in cs else None))
    groups = {state:dict(count=sum(r['status']==state for r in rows),slot_ids=[r['slot_id'] for r in rows if r['status']==state]) for state in STATES}
    return dict(schema_version=REPORT_VERSION,suite=suite,
        purpose='candidate_business_release' if suite=='release' else 'harness_selftest_only',
        status=worst([r['status'] for r in rows]+[c['status'] for c in controls]),
        policy=effective,policy_sha256=digest(effective),planned=len(ids),slots=rows,controls=controls,counts=groups,
        baseline=dict(directory=baseline['directory'],manifest_sha256=digest(bm),source_sha256=bm['source_sha256']) if bm else None,
        candidate=dict(directory=candidate['directory'],manifest_sha256=digest(cm),source_sha256=cm['source_sha256']),
        model_requests=0,scope='Registered D31 deterministic profiles; no production deployment or model-quality claim')

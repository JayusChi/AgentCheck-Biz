"""No imports, commands, SQL, target URLs or plugin paths supplied by a manifest."""
from copy import deepcopy
from importlib.metadata import version
import json
import os
from pathlib import Path
from uuid import uuid4
import hashlib
from jsonschema import Draft202012Validator
from agentcheck_biz.checks import load_json
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.persistence.store import digest, PINS
from agentcheck_biz.network_acceptance.profiles import profiles
from agentcheck_biz.continuity.demo import scenarios
from . import BACKEND_VERSION, API_VERSION, MANIFEST_VERSION, EVIDENCE_VERSION

SCHEMA = REPO_ROOT / 'schema/v2-manifest.schema.json'


def source_files():
    roots = ['agentcheck_biz', 'examples/ticket_agent', 'examples/ticket_http',
             'examples/gitea_target', 'examples/business_mcp', 'schema', 'cases']
    return {p.relative_to(REPO_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for root in roots for p in sorted((REPO_ROOT / root).rglob('*'))
            if p.is_file() and p.suffix in {'.py', '.sql', '.json'}}


def source_digest():
    return digest(source_files())


def catalog():
    result = {}
    for engine, plans in [('network', profiles()), ('continuity', scenarios())]:
        for plan in plans:
            target = plan['target'] if engine == 'network' else plan['kind']
            profile = engine + ':' + (plan['id'] if engine == 'network' else plan['name'])
            case = load_json(REPO_ROOT / ('cases/gitea/G01.json' if target == 'gitea' else 'cases/tickets/full/T01.json'))
            if engine == 'continuity':
                fault = {k: plan[k] for k in ('window', 'action', 'fault')}
                recovery = dict(version='continuity/1', client='offline-fixed/1')
                budget = {k: plan[k] for k in ('model_limit', 'tool_limit', 'wall_seconds')}
                expected = dict(agent_status=plan['expected'], resource_count=plan['count'],
                    business_status='PASS' if plan['expected']=='PASS' or plan['name']=='final-model-budget' else 'INCONCLUSIVE',
                    evidence_status='PASS', coverage='covered',
                    observation_status='failed' if plan['fault']=='unavailable' else 'complete')
                adapter = target + '-continuity/1'
                app_version = 'fixed'
            else:
                kind = plan['kind']
                app_version = plan.get('version') or ('unsafe' if kind=='recovery' else 'fixed')
                fault = dict(kind=kind)
                if kind == 'proxy':
                    if plan['scenario']=='commit_loss':
                        fault['rule'] = load_json(REPO_ROOT / ('cases/commit_loss/'+target+'.json')) if plan['fault'] else None
                    else:
                        fault['rule'] = load_json(REPO_ROOT / ('cases/proxy/'+plan['scenario']+'.json'))['rule']
                        if fault['rule']:fault['rule']['tool'] = 'create_issue' if target=='gitea' else 'create_ticket'
                elif kind=='crash':fault['point'] = plan['point']
                else:fault['rule'] = load_json(REPO_ROOT / 'cases/commit_loss/ticket.json')
                recovery = dict(version='recovery-policy/1' if kind in {'recovery','crash'} else 'scripted/1',
                                strategy='verify_first' if kind in {'recovery','crash'} else 'fixed-case')
                budget = dict(model_limit=0, tool_limit=plan.get('maximum',case['limits']['max_tool_calls']),
                              wall_seconds=45 if kind in {'recovery','crash'} else 90)
                expected = plan['expected'] | dict(observation_status='complete')
                adapter = target + ('-mcp/1' if kind=='proxy' else '-http/1')
            result[profile] = dict(profile=profile, engine=engine,
                object=dict(kind=target, version='1.26.4' if target=='gitea' else app_version),
                adapter=dict(version=adapter, protocol='HTTP/1.1' if 'mcp' not in adapter else 'MCP/2025-11-25/stdio+HTTP/1.1'),
                case=case, initial_state=case['initial_issues' if target=='gitea' else 'initial_tickets'],
                fault=fault, recovery=recovery, budget=budget | dict(slot_timeout_seconds=240), expected=expected)
    return result


def capabilities(*, resources=False):
    result = dict(backend_version=BACKEND_VERSION, api_version=API_VERSION,
        manifest_versions=[MANIFEST_VERSION], evidence_versions=[EVIDENCE_VERSION],
        features={'business_runs':1, 'comparison':1, 'recovery_evidence':1, 'manifest_cli':2},
        execution=dict(serial=True, parallel_max=2, platform='win32', model_requests=0),
        profiles=list(catalog()), source_sha256=source_digest())
    if resources:
        from agentcheck_biz.persistence.runtime import DEFAULT_BINARY as PG, ARCHIVE_SHA256
        from examples.gitea_target.runtime import DEFAULT_BINARY as GITEA, BINARY_SHA256
        result['resources'] = dict(platform=os.name=='nt', packages={k:version(k) for k in PINS},
            mcp=version('mcp'), gitea=GITEA.is_file() and hashlib.sha256(GITEA.read_bytes()).hexdigest()==BINARY_SHA256,
            postgres=PG.is_file() and hashlib.sha256((PG.parents[2]/'postgres-binaries.jar').read_bytes()).hexdigest()==ARCHIVE_SHA256)
    return result


def validate(value, *, environment=True, current_source=True):
    errors = sorted(Draft202012Validator(load_json(SCHEMA)).iter_errors(value), key=lambda e:str(list(e.path)))
    if errors:raise ValueError('Invalid manifest: '+str(list(errors[0].path))+': '+errors[0].message)
    known = catalog()
    if type(value['execution']['parallel']) is not int:raise ValueError('parallel must be an integer JSON value')
    ids = [s['slot_id'] for s in value['slots']]
    if len(set(ids))!=len(ids):raise ValueError('Duplicate slot_id')
    if current_source and value['source_sha256']!=source_digest():raise ValueError('Source digest mismatch; freeze a new manifest')
    for slot in value['slots']:
        if slot['profile'] not in known or digest({k:v for k,v in slot.items() if k!='slot_id'})!=digest(known[slot['profile']]):
            raise ValueError('Unsupported or changed frozen profile: '+slot['slot_id'])
    if environment:
        resource = capabilities(resources=True)['resources']
        if not resource['platform'] or resource['packages']!=PINS or resource['mcp']!='1.30.0':
            raise ValueError('Unsupported platform or dependency versions')
        # Continuity suite owns a Gitea runtime even for its Ticket scenarios.
        if any(s['engine']=='continuity' for s in value['slots']) and not resource['postgres']:
            raise ValueError('Pinned PostgreSQL runtime unavailable')
        if any(s['object']['kind']=='gitea' or s['engine']=='continuity' for s in value['slots']) and not resource['gitea']:
            raise ValueError('Pinned Gitea runtime unavailable')
    return value


def build(selected, *, parallel=1):
    known = catalog()
    return dict(schema_version=MANIFEST_VERSION, evidence_version=EVIDENCE_VERSION,
        manifest_id=str(uuid4()), source_sha256=source_digest(), execution=dict(parallel=parallel),
        slots=[dict(slot_id=f's{i+1:03}', **deepcopy(known[name])) for i,name in enumerate(selected)])


def read(path):
    def pairs(values):
        result={}
        for k,v in values:
            if k in result:raise ValueError('Duplicate JSON key: '+k)
            result[k]=v
        return result
    path=Path(path)
    if path.stat().st_size>2_000_000:raise ValueError('Manifest exceeds 2 MB')
    return json.loads(path.read_text(encoding='utf8'),object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON number')))

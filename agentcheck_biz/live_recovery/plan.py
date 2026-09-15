"""Credential-free, complete request preview and single-use authorization binding."""
from copy import deepcopy
from importlib.metadata import version
from pathlib import Path
import hashlib
from agentcheck_biz.checks import load_json
from agentcheck_biz.experiment import ENDPOINT
from agentcheck_biz.persistence.store import digest
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from examples.ticket_agent.llm_agent import SYSTEM_PROMPT, TOOLS

LIMITS = dict(model_limit=5, tool_limit=6, wall_seconds=90)


def source_digest():
    files = [REPO_ROOT/'pipeline/bailian.py', REPO_ROOT/'requirements.txt',
             REPO_ROOT/'requirements-persistence.txt', REPO_ROOT/'requirements-acceptance.txt']
    files += sorted((REPO_ROOT/'agentcheck_biz').rglob('*.sql'))
    return digest(dict(python=implementation_digest(), files={
        p.relative_to(REPO_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}))


def make_plan():
    case = load_json(REPO_ROOT/'cases/tickets/full/T01.json')
    return dict(schema='d34-live/1', endpoint=ENDPOINT, model='qwen3.7-max', sdk_version=version('openai'),
        source_sha256=source_digest(), case=case, limits=LIMITS,
        request_timeout_seconds=30, parent_timeout_seconds=180, maximum_model_requests=30,
        recovery='saved model messages; unresolved dispatched tool becomes outcome_unknown; model chooses query/retry; identical A/B/C',
        first_request=dict(model='qwen3.7-max', messages=[dict(role='system',content=SYSTEM_PROMPT),
            dict(role='user',content=case['task'])], tools=deepcopy(TOOLS), temperature=0,
            max_tokens=512, extra_body=dict(enable_thinking=False)),
        sdk_retries=0,
        subsequent_data=['previous model replies and tool call IDs',
            'synthetic scoped ticket rows: tenant-A, repair-001, C001, D001, 无法开机, generated ticket ID, open',
            'tool status: ok, outcome_unknown, invalid_arguments, conflict; recovery uncertainty'],
        excluded_data=['API key in Authorization header only, never prompt or artifact',
            'no local files, database credentials, HTTP credentials, unrelated tenant rows'],
        billing='30 requests and 512 output tokens/request are quantity limits, NOT a monetary cap. Input tokens and fees are uncapped; unknown usage is not zero; provider bill is authoritative.',
        stop_policy='First ERROR or INCONCLUSIVE stops all later slots. FAIL is retained. No replacement or paid rerun under this authorization.',
        slots=[dict(slot_id=f'{family}-{phase}', family=family, phase=phase,
                    app_version='fixed' if phase=='C' else 'unsafe', fault=phase!='A')
               for family in ('response-loss','agent-crash') for phase in 'ABC'])


def validate(plan):
    if plan != make_plan():
        raise ValueError('Preview differs from current source, case, SDK or frozen experiment')


def authorize(plan, record, output):
    validate(plan)
    expected = dict(schema='d34-authorization/1', plan_sha256=digest(plan),
        output=str(Path(output).resolve()), maximum_model_requests=30,
        accept_uncapped_input_and_fees=True, approved=True)
    if any(record.get(k) != v for k,v in expected.items()) or not record.get('user_authorization'):
        raise ValueError('Current explicit authorization must bind this preview, output and uncapped fees')
    # Authorizations cannot be recycled by selecting a different output folder.
    if Path(output).exists():
        raise ValueError('Experiment output already exists; resume/replacement is forbidden')

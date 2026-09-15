"""Finite model worker; credentials arrive via stdin, never in saved state."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time
import httpx
from typing import TypedDict
from uuid import uuid4
from langgraph.graph import StateGraph, START, END
from langsmith import tracing_context
from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.adapters.http_transport import HttpTransport, HttpTimeouts
from agentcheck_biz.checks import load_json
from agentcheck_biz.continuity.store import Budgets, Stopped
from agentcheck_biz.events import EventLog
from agentcheck_biz.persistence.graph import config, snapshot_payload
from agentcheck_biz.persistence.store import BindingError, IDENTITY_KEYS
from agentcheck_biz.reports import save_json
from agentcheck_biz.scheduling.store import Scheduler
from agentcheck_biz.scheduling.worker import renewing
from agentcheck_biz.side_effects.store import Operations
from .plan import LIMITS, validate


class LiveBudgets(Budgets):
    def create(self, manifest):
        if manifest['recovery'] != LIMITS | dict(version='continuity/1',client='d34-live/1'):
            raise BindingError('D34 budget is immutable')
        validate(manifest['experiment_plan'])
        return Operations.create(self,manifest)


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
    messages: list
    pending: list
    completed: bool


def fake_response(messages, retry=False):
    """Explicit offline provider; no fallback from the live provider."""
    tools=[m for m in messages if m['role']=='tool']
    name,args='create_ticket',dict(customer_id='C001',device_id='D001',description='无法开机')
    if tools:
        result=json.loads(tools[-1]['content'])
        if result['status']=='ok':
            rows=result['result'] if isinstance(result['result'],list) else [result['result']]
            return dict(role='assistant',content=json.dumps(dict(status='completed',ticket_id=rows[0]['ticket_id'])))
        if not retry: name,args='query_tickets',{}
    return dict(role='assistant',content=None,tool_calls=[dict(id='fake-'+str(len(messages)),type='function',
        function=dict(name=name,arguments=json.dumps(args)))])


def build(saver, manifest, store=None, lease=None, connection=None, output=None, action=None):
    plan=manifest['experiment_plan']
    def active(state):
        if any(state.get(k)!=manifest[k] for k in IDENTITY_KEYS):
            raise BindingError('Foreign checkpoint')
        return store.active(manifest,lease)

    def model(state):
        deadline=active(state)
        receipt=store.charge(manifest,lease,'model',dict(client='d34-live/1',message_count=len(state['messages'])))
        path=output/('model-'+receipt['call_id']+'.json')
        request=deepcopy(plan['first_request']); request['messages']=state['messages']
        record=dict(reservation=receipt,request=request,mode=connection['mode'],usage=None,response=None)
        save_json(path,record)
        try:
            if connection['mode']=='offline-test':
                answer=fake_response(state['messages'],connection.get('retry',False))
                record.update(response_model='offline-fixture',usage=dict(prompt_tokens=0,completion_tokens=0,total_tokens=0))
            else:
                from openai import OpenAI
                import httpx
                # Explicit client disables inherited proxies and redirect followups.
                with OpenAI(api_key=connection['model_key'],base_url=plan['endpoint'],max_retries=0,
                    timeout=min(30,max(.01,deadline-time.time())),
                    http_client=httpx.Client(trust_env=False,follow_redirects=False)) as client:
                    response=client.chat.completions.create(**request)
                message=response.choices[0].message
                answer=dict(role='assistant',content=message.content)
                if message.tool_calls:
                    answer['tool_calls']=[dict(id=c.id,type='function',function=dict(name=c.function.name,arguments=c.function.arguments)) for c in message.tool_calls]
                record.update(response_model=response.model,finish_reason=response.choices[0].finish_reason,
                    usage=response.usage.model_dump() if response.usage else None)
            record['response']=answer
            save_json(path,record)
            active(state)
            calls=answer.get('tool_calls',[])
            if len({c['id'] for c in calls})!=len(calls):raise BindingError('Duplicate tool call IDs')
            previous={c['id'] for m in state['messages'] for c in m.get('tool_calls',[])}
            if any(c['id'] in previous for c in calls):raise BindingError('Reused tool call ID')
            return dict(messages=state['messages']+[answer],pending=calls,completed=not calls)
        except Exception as exc:
            record['error_type']=type(exc).__name__; save_json(path,record)
            raise

    def tool(state):
        deadline=active(state)
        call=state['pending'][0]
        # A previously dispatched, uncheckpointed call is never blindly repeated.
        old=[e for e in store.budget(manifest)['events'] if e['kind']=='tool' and e['detail'].get('model_tool_id')==call['id']]
        if old:
            result=dict(status='outcome_unknown',message='Worker interrupted after dispatch; verify before claiming completion')
        else:
            receipt=store.charge(manifest,lease,'tool',dict(model_tool_id=call['id'],name=call['function']['name']))
            result=dict(status='invalid_arguments')
            try:
                args=json.loads(call['function']['arguments'])
                name=call['function']['name']
                valid=(name=='query_tickets' and args=={}) or (name=='create_ticket' and args==plan['case']['request'])
                if valid:
                    context=RunContext(manifest['run_id'],manifest['operation_id'],lease['attempt_id'],deadline,Path(manifest['evidence_dir']))
                    transport=HttpTransport(connection['origin'],context,EventLog(output/('http-'+receipt['call_id']+'.jsonl'),context.run_id),HttpTimeouts())
                    status,value=transport.request('POST' if name=='create_ticket' else 'GET','/tickets',
                        token=connection['token'],request_id=receipt['call_id'],operation_id=manifest['operation_id'],
                        body=args if name=='create_ticket' else None)
                    result=dict(status='ok',result=value) if status==200 else dict(status='conflict' if status==409 else 'outcome_unknown')
                    if status==200 and name=='create_ticket' and action=='crash':
                        save_json(output/'barrier.json',dict(pid=os.getpid(),job_id=manifest['job_id'],
                            window='after_commit',call_id=receipt['call_id'],ticket=value))
                        # Controller independently observes SQL and kills this owned process.
                        while time.time()<deadline:time.sleep(.03)
                        raise Stopped('Controller did not terminate crash worker')
            except (json.JSONDecodeError,TypeError):
                result=dict(status='invalid_arguments')
            except Stopped: raise
            except (httpx.HTTPError,TimeoutError) as exc:
                result=dict(status='outcome_unknown',error_type=type(exc).__name__)
        active(state)
        message=dict(role='tool',tool_call_id=call['id'],content=json.dumps(result,ensure_ascii=False))
        save_json(output/('tool-'+str(uuid4())+'.json'),dict(call=call,result=result,recovered=bool(old)))
        return dict(messages=state['messages']+[message],pending=state['pending'][1:])

    flow=StateGraph(State)
    flow.add_node('model',model); flow.add_node('tool',tool)
    flow.add_edge(START,'model')
    flow.add_conditional_edges('model',lambda s: END if s['completed'] else 'tool')
    flow.add_conditional_edges('tool',lambda s:'tool' if s['pending'] else 'model')
    return flow.compile(checkpointer=saver)


def run(manifest,output,connection,action):
    validate(manifest['experiment_plan'])
    if connection.get('mode') not in {'live','offline-test'}:raise BindingError('Explicit provider mode required')
    if connection['mode']!=manifest['mode']:raise BindingError('Provider mode differs from saved job')
    if connection['mode']=='live' and not connection.get('model_key'):raise BindingError('Missing live capability')
    store=LiveBudgets(connection['dsn'])
    lease=store.claim(manifest,str(uuid4()),3 if action=='crash' else 30)
    if not lease:raise BindingError('Job already owned')
    save_json(output/'claim.json',lease)
    try:
        with renewing(store,manifest,lease,3 if action=='crash' else 30),store.saver(manifest,lease) as saver,tracing_context(enabled=False):
            flow=build(saver,manifest,store,lease,connection,output,action)
            cfg=config(manifest,lease['attempt_id']); cfg['recursion_limit']=30
            if action=='recover':
                saved=flow.get_state(cfg)
                if not lease['takeover'] or not saved.values or not saved.next:
                    raise BindingError('Recovery requires an unfinished original checkpoint')
                save_json(output/'checkpoint-before.json',snapshot_payload(saved))
            initial={k:manifest[k] for k in IDENTITY_KEYS}|dict(messages=deepcopy(manifest['experiment_plan']['first_request']['messages']),pending=[],completed=False)
            flow.invoke(None if action=='recover' else initial,cfg,durability='sync')
            saved=flow.get_state(cfg)
            save_json(output/'checkpoint-after.json',snapshot_payload(saved))
            answer=json.loads(saved.values['messages'][-1]['content'])
            if not isinstance(answer,dict) or answer.get('status') not in {'completed','blocked','needs_verification'}:
                raise BindingError('Final response is not the structured protocol')
            Scheduler.finish(store,manifest,lease,'finished',saved.config['configurable']['checkpoint_id'],'agent_final_saved_business_verdict_separate')
            return dict(status='PASS',answer=answer)
    except Exception as exc:
        try:Scheduler.finish(store,manifest,lease,'waiting_verification',None,'requires_verification')
        except Exception:pass
        return dict(status='INCONCLUSIVE' if isinstance(exc,(Stopped,BindingError,json.JSONDecodeError)) else 'ERROR',error_type=type(exc).__name__)


def main():
    manifest=load_json(Path(sys.argv[1])); output=Path(sys.argv[2]); output.mkdir()
    try:result=run(manifest,output,json.loads(sys.stdin.readline()),sys.argv[3])
    except Exception as exc:result=dict(status='ERROR',error_type=type(exc).__name__)
    result['pid']=os.getpid(); save_json(output/'result.json',result)
    return dict(PASS=0,FAIL=1,INCONCLUSIVE=2,ERROR=3)[result['status']]


if __name__=='__main__':raise SystemExit(main())

"""Real AIMessage tool_calls -> ToolMessage -> final Agent turn, offline injected client."""
import json
from typing import TypedDict
from langchain_core.messages import AIMessage, ToolMessage, messages_to_dict, messages_from_dict
from langgraph.graph import StateGraph, START, END
from agentcheck_biz.persistence.store import BindingError, IDENTITY_KEYS
from agentcheck_biz.side_effects.policy import Recovery
from .targets import Ticket, Gitea


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
    recovery_version: str
    phase: str
    messages: list[dict]
    result: dict | None
    model_requests: int


def tool_call(manifest):
    return dict(name='ensure_operation',args=dict(operation_id=manifest['operation_id'],
                request=manifest['side_effect']['request']),id='operation-'+manifest['job_id'],type='tool_call')


class OfflineModel:
    """No SDK, network, credentials or fallback; deterministic, replaceable in tests."""
    def invoke(self, messages, manifest):
        if not messages:
            return AIMessage(content='',tool_calls=[tool_call(manifest)])
        if not isinstance(messages[-1],ToolMessage) or messages[-1].tool_call_id != tool_call(manifest)['id']:
            raise BindingError('Final model turn requires the matching ToolMessage')
        return AIMessage(content='Operation independently confirmed.')


def validate(state, manifest):
    if any(state.get(k) != manifest[k] for k in IDENTITY_KEYS) or state.get('recovery_version') != 'continuity/1' or state.get('model_requests') != 0:
        raise BindingError('Foreign or missing Agent checkpoint')
    messages = messages_from_dict(state['messages'])
    expected_length={'new':0,'tool_pending':1,'tool_verified':2,'completed':3}.get(state.get('phase'))
    if expected_length is None or len(messages)!=expected_length:
        raise BindingError('Checkpoint phase and Agent message sequence disagree')
    if messages and (not isinstance(messages[0],AIMessage) or messages[0].tool_calls != [tool_call(manifest)]):
        raise BindingError('Tool name, arguments and ID must match the frozen operation')
    if len(messages)>1 and (not isinstance(messages[1],ToolMessage) or messages[1].tool_call_id != tool_call(manifest)['id']
                            or json.loads(messages[1].content) != state['result']):
        raise BindingError('Tool result does not match saved business result')
    if len(messages)==3 and (not isinstance(messages[2],AIMessage) or messages[2].tool_calls):
        raise BindingError('Completed Agent requires a final response without new tools')
    return messages


def graph(saver, manifest, store=None, lease=None, output=None, connection=None, events=None,
          model=None, gate=lambda window: None, pause=False):
    model = model or OfflineModel()
    def infer(state, final=False):
        messages = validate(state,manifest)
        receipt = store.charge(manifest,lease,'model',dict(client='offline-fixed/1',phase='final' if final else 'tool_selection'))
        events.record('dispatch',**receipt,client='offline-fixed/1')
        gate('model_reserved')
        store.active(manifest,lease)
        answer = model.invoke(messages,manifest)
        store.active(manifest,lease)
        if not isinstance(answer,AIMessage) or (not final and answer.tool_calls != [tool_call(manifest)]) or (final and answer.tool_calls):
            raise BindingError('Offline client returned an invalid tool protocol')
        events.record('return',call_id=receipt['call_id'])
        return dict(messages=messages_to_dict(messages+[answer]),phase='completed' if final else 'tool_pending')
    def execute(state):
        messages = validate(state,manifest)
        if len(messages)!=1:
            raise BindingError('Expected one saved tool selection')
        target = (Ticket if manifest['side_effect']['kind']=='ticket' else Gitea)(manifest,lease,output,connection,store,events)
        result = Recovery(store,manifest,lease,target,gate=gate).apply()
        store.active(manifest,lease)
        return dict(result=result,phase='tool_verified',messages=messages_to_dict(messages+[
            ToolMessage(content=json.dumps(result,sort_keys=True),tool_call_id=messages[0].tool_calls[0]['id'],name='ensure_operation')]))
    flow=StateGraph(State)
    flow.add_node('model',infer)
    flow.add_node('tools',execute)
    flow.add_node('final_model',lambda state:infer(state,True))
    for a,b in ((START,'model'),('model','tools'),('tools','final_model'),('final_model',END)):
        flow.add_edge(a,b)
    return flow.compile(checkpointer=saver,interrupt_after=['model'] if pause else None)

"""Every Agent HTTP request, including identity checks and reads, spends budget."""
from dataclasses import replace
from pathlib import Path
from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.adapters.http_transport import HttpTransport, HttpTimeouts
from agentcheck_biz.events import EventLog
from agentcheck_biz.persistence.store import BindingError
from agentcheck_biz.side_effects.targets import Ticket as BaseTicket, Gitea as BaseGitea


class Metered:
    def __init__(self, inner, store, manifest, lease, events):
        self.inner, self.store, self.manifest, self.lease, self.events = inner,store,manifest,lease,events
        self.origin = inner.origin

    def request(self, method, path, **kwargs):
        reservation = self.store.charge(self.manifest,self.lease,'tool',dict(method=method,path=path,origin=self.origin))
        # Use the original absolute deadline, never a new timeout on restart.
        self.inner.context = replace(self.inner.context,deadline=reservation['deadline'])
        self.events.record('dispatch',**reservation,method=method,path=path,origin=self.origin)
        result = self.inner.request(method,path,**kwargs)
        self.store.active(self.manifest,self.lease)
        self.events.record('return',call_id=reservation['call_id'])
        return result


class Ticket(BaseTicket):
    def __init__(self, manifest, lease, output, connection, store, events):
        self.op,self.output,self.connection = manifest['side_effect'],output,connection
        context = RunContext(manifest['run_id'],manifest['operation_id'],lease['attempt_id'],
                             store.active(manifest,lease),Path(manifest['evidence_dir']))
        def transport(origin, file):
            return Metered(HttpTransport(origin,context,EventLog(output/file,context.run_id),HttpTimeouts()),
                           store,manifest,lease,events)
        self.client = transport(connection['origin'],'http-client.jsonl')
        self.observer = transport(connection.get('observation_origin',connection['origin']),'http-observer.jsonl')
        self.number = 0
        status, identity = self.client.request('GET','/version',token=connection['token'],request_id='version')
        if status != 200 or identity != self.op['target'] or identity['app_version'] != 'fixed':
            raise BindingError('Atomic replay requires the frozen fixed Ticket service')
        self.expected = self.op['request'] | dict(tenant_id=self.op['scope'],operation_id=manifest['operation_id'],status='open')


class Gitea(BaseGitea):
    def __init__(self, manifest, lease, output, connection, store, events):
        super().__init__(manifest,lease,output,connection)
        self.context = replace(self.context,deadline=store.active(manifest,lease))
        self.client.context = self.context
        self.client = Metered(self.client,store,manifest,lease,events)
        reader = self.observer.environment.observer_client
        reader.context = self.context
        self.observer.environment.observer_client = Metered(reader,store,manifest,lease,events)

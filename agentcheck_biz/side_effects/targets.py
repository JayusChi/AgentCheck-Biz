"""Object-specific write contracts and independent, complete read evidence."""
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import time
from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.adapters.http_transport import HttpTransport, HttpTimeouts
from agentcheck_biz.adapters.gitea_client import GiteaSettings, GiteaClient
from agentcheck_biz.observers.gitea import GiteaObserver
from agentcheck_biz.events import EventLog
from agentcheck_biz.persistence.store import BindingError
from agentcheck_biz.reports import save_json


class Ticket:
    atomic_idempotency = True

    def __init__(self, manifest, lease, output, connection):
        self.op, self.output, self.connection = manifest['side_effect'], output, connection
        context = RunContext(manifest['run_id'], manifest['operation_id'], lease['attempt_id'], time.time()+90,
                             Path(manifest['evidence_dir']))
        self.client = HttpTransport(connection['origin'], context, EventLog(output/'http-client.jsonl', context.run_id), HttpTimeouts())
        self.observer = HttpTransport(connection.get('observation_origin', connection['origin']), context,
            EventLog(output/'http-observer.jsonl', context.run_id), HttpTimeouts())
        self.number = 0
        status, identity = self.client.request('GET', '/version', token=connection['token'], request_id='version')
        if status != 200 or identity != self.op['target'] or identity['app_version'] != 'fixed':
            raise BindingError('Atomic replay requires the actually verified fixed service identity')
        self.expected = self.op['request'] | dict(tenant_id=self.op['scope'], operation_id=manifest['operation_id'], status='open')

    def create(self):
        self.number += 1
        status, result = self.client.request('POST', '/tickets', token=self.connection['token'], request_id=f'create-{self.number}',
            operation_id=self.op['operation_id'], body=self.op['request'])
        if status != 200 or not isinstance(result, dict) or not result.get('ticket_id'):
            raise RuntimeError('Ticket create did not confirm a matching response')
        return result

    def observe(self):
        try:
            self.number += 1
            status, items = self.observer.request('GET', '/tickets', token=self.connection['token'],
                request_id=f'observe-{self.number}', operation_id=self.op['operation_id'])
            complete = status == 200 and isinstance(items, list) and all(isinstance(r, dict) and r.get('ticket_id') for r in items)
            value = dict(complete=bool(complete), items=items if complete else None, http_status=status, source='ticket-http-query')
        except Exception as exc:
            value = dict(complete=False, items=None, error_type=type(exc).__name__, source='ticket-http-query')
        save_json(self.output/f'observation-{self.number}.json', value)
        return value


class Gitea:
    atomic_idempotency = False

    def __init__(self, manifest, lease, output, connection):
        self.op, self.output = manifest['side_effect'], output
        context = RunContext(output.name, manifest['operation_id'], lease['attempt_id'], time.time()+90, output)
        settings = GiteaSettings(**connection['settings'])
        if settings.public() != self.op['target']:
            raise BindingError('Gitea target differs from the frozen operation')
        self.client = GiteaClient(settings, 'execution', context)
        reader = GiteaClient(replace(settings, origin=connection.get('observation_origin', settings.origin)), 'observer', context)
        self.repository, self.context = self.op['repository'], context
        self.path = '/api/v1/repos/' + self.repository['full_name']
        environment = SimpleNamespace(repository=self.repository, repo_path=self.path, observer_client=reader)
        self.observer = GiteaObserver(environment, dict(limits=dict(page_size=20, max_pages=10)))
        self.expected = self.op['request'] | dict(state='open', repository_id=self.repository['id'], repository=self.repository['full_name'])
        self.number = 0

    def create(self):
        return self.client.request('POST', self.path+'/issues', body=self.op['request'], expected=201)['body']

    def observe(self):
        self.number += 1
        observation = self.observer.observe(self.context)
        raw = observation.to_dict()
        items = [r for r in observation.data['issues'] if self.op['marker'] in r['body']] if observation.complete else None
        value = dict(complete=observation.complete, items=items, source='gitea-api-readonly', observation=raw)
        save_json(self.output/f'observation-{self.number}.json', value)
        return value

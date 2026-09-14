"""Real PG, fixed Ticket, official Gitea, owned Agent kills and concurrent recovery."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4
from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.adapters.ticket_http import TicketHttpEnvironment
from agentcheck_biz.adapters.http_transport import HttpTimeouts
from agentcheck_biz.adapters.gitea import GiteaEnvironment
from agentcheck_biz.adapters.gitea_client import GiteaSettings
from agentcheck_biz.observers.gitea import GiteaObserver
from agentcheck_biz.gitea_cases import issue_body, marker
from agentcheck_biz.events import EventLog
from agentcheck_biz.checks import load_json, observe_database
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.persistence.runtime import PostgresRuntime, child_environment
from agentcheck_biz.persistence.store import new_manifest, digest
from agentcheck_biz.persistence.graph import checkpointer, config, snapshot_payload
from agentcheck_biz.scheduling.demo import wait_expiry
from agentcheck_biz.agent_crash.demo import Agent
from agentcheck_biz.reports import save_json
from examples.gitea_target.runtime import GiteaRuntime, DEFAULT_INSTANCE_ROOT
from examples.gitea_target.demo import target_environment
from .store import Operations
from .worker import graph


def scenarios():
    return [dict(name=f'ticket-{strategy}-{n}', kind='ticket', window='after_commit', strategy=strategy,
                 fault='none', expected='PASS', count=1) for n in range(1,4) for strategy in ('query','replay')] + [
        dict(name='ticket-before', kind='ticket', window='before_call', strategy='query', fault='none', expected='PASS', count=1),
        dict(name='ticket-saved', kind='ticket', window='after_checkpoint', strategy='query', fault='none', expected='PASS', count=1),
        dict(name='ticket-concurrent', kind='ticket', window='after_commit', strategy='query', fault='concurrent', expected='PASS', count=1),
        dict(name='ticket-unavailable', kind='ticket', window='after_commit', strategy='query', fault='unavailable', expected='INCONCLUSIVE', count=1),
        dict(name='ticket-unknown-empty', kind='ticket', window='sent_unknown', strategy='query', fault='none', expected='PASS', count=1),
        dict(name='gitea-confirmed', kind='gitea', window='after_commit', strategy='query', fault='none', expected='PASS', count=1),
        dict(name='gitea-empty', kind='gitea', window='sent_unknown', strategy='query', fault='none', expected='INCONCLUSIVE', count=0),
        dict(name='gitea-duplicate', kind='gitea', window='after_commit', strategy='query', fault='duplicate', expected='INCONCLUSIVE', count=2),
        dict(name='gitea-unavailable', kind='gitea', window='after_commit', strategy='query', fault='unavailable', expected='INCONCLUSIVE', count=1),
        dict(name='gitea-concurrent', kind='gitea', window='after_commit', strategy='query', fault='concurrent', expected='PASS', count=1)]


def spawn(module, args, data, log):
    env = child_environment(dict(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1'))
    executable = sys.executable
    if os.name == 'nt':
        executable = sys._base_executable
        env['__PYVENV_LAUNCHER__'] = sys.executable
    process = subprocess.Popen([executable, '-X','utf8','-m',module,*args], cwd=REPO_ROOT, env=env,
        stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    process.stdin.write((json.dumps(data)+'\n').encode())
    process.stdin.close()
    return process


class OperationAgent(Agent):
    def __init__(self, exp, action, label=None, **overrides):
        self.exp, self.action = exp, action
        self.output = exp.directory/(label or action)
        self.log = self.output.with_suffix('.log').open('wb')
        self.process = spawn('agentcheck_biz.side_effects.worker', ['--manifest',str(exp.manifest_path),
            '--output',str(self.output),'--action',action,'--test-config',str(exp.spec_path)],
            exp.connection | overrides, self.log)


class Outage:
    def __init__(self, directory):
        self.directory = directory
        self.log = (directory/'outage-process.log').open('wb')
        self.process = spawn('agentcheck_biz.side_effects.read_outage', [], dict(directory=str(directory), test_only=True), self.log)
        deadline = time.monotonic()+10
        while not (directory/'outage-ready.json').exists():
            if self.process.poll() is not None or time.monotonic()>deadline:
                self.close()
                raise RuntimeError('Read outage did not start')
            time.sleep(.03)
        self.origin = load_json(directory/'outage-ready.json')['origin']

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.log.close()
        save_json(self.directory/'outage-cleanup.json',dict(pid=self.process.pid, exited=True, exit_code=self.process.returncode))


class Experiment:
    def __init__(self, root, store, scenario, gitea):
        self.directory = root/('operation-'+uuid4().hex)
        self.directory.mkdir()
        self.scenario, self.store, self.gitea = scenario, store, gitea
        self.case = load_json(REPO_ROOT/('cases/tickets/full/T01.json' if scenario['kind']=='ticket' else 'cases/gitea/G01.json'))
        operation = self.case['context']['operation_id'] if scenario['kind']=='ticket' else self.case['operation_id']
        self.context = RunContext(self.directory.name, operation, 'environment-'+uuid4().hex, time.time()+600, self.directory)
        self.events = EventLog(self.directory/'events.jsonl', self.directory.name)
        self.service = (TicketHttpEnvironment(self.case,'fixed',HttpTimeouts()) if scenario['kind']=='ticket'
            else GiteaEnvironment(GiteaSettings.from_environment(), self.case))

    def __enter__(self):
        try:
            self.service.prepare(self.context,self.events)
            self.initial = self.observe()
            save_json(self.directory/'initial.json', self.initial)
            if self.scenario['kind']=='ticket':
                case = self.case
                op = dict(kind='ticket', scope=case['context']['tenant_id'], operation_id=self.context.operation_id,
                    request=case['request'], target=self.service.identity)
                connection = dict(origin=self.service.transport.origin, token=self.service.tokens['tenant-A'])
            else:
                case = dict(context=dict(operation_id=self.context.operation_id), request=self.case['request'])
                op = dict(kind='gitea', scope=self.service.repository['full_name'], operation_id=self.context.operation_id,
                    request=dict(title=self.case['request']['title'], body=issue_body(self.context,self.context.operation_id,self.case['request']['body'])),
                    target=self.service.settings.public(), repository=self.service.repository, marker=marker(self.context,self.context.operation_id))
                connection = dict(settings=asdict(self.service.settings))
            self.manifest = new_manifest(self.directory, case) | dict(side_effect=op)
            self.manifest_path = self.directory/'manifest.json'
            save_json(self.manifest_path,self.manifest)
            self.store.create(self.manifest)
            self.spec = dict(test_only=True, job_id=self.manifest['job_id'], window=self.scenario['window'])
            self.spec_path = self.directory/'test-config.json'
            save_json(self.spec_path,self.spec)
            self.connection = connection | dict(dsn=self.store.dsn, retry_unknown=self.scenario['strategy']=='replay')
            return self
        except BaseException:
            self.service.cleanup(self.context,self.events)
            raise

    def observe(self):
        if self.scenario['kind']=='ticket':
            return observe_database(self.directory/'business.sqlite')
        observation = GiteaObserver(self.service,self.case).observe(self.context)
        if not observation.complete:
            raise RuntimeError('Independent Gitea observation failed')
        return observation.to_dict()

    def targets(self, observed):
        if self.scenario['kind']=='ticket':
            return [r for r in observed['tickets'] if r['tenant_id']=='tenant-A' and r['operation_id']==self.context.operation_id]
        return [r for r in observed['data']['issues'] if marker(self.context,self.context.operation_id) in r['body']]

    def capture(self,label):
        with checkpointer(self.store.dsn, readonly=True) as saver:
            state = graph(saver).get_state(config(self.manifest))
            checkpoint = snapshot_payload(state) if state.values else None
        data = dict(checkpoint=checkpoint, checkpoint_sha256=digest(checkpoint), ledger=self.store.read(self.manifest),
                    business=self.observe(), postgres=self.store.export())
        save_json(self.directory/(label+'.json'),data)
        return data

    def alive(self, pg, label):
        with self.store.connect(readonly=True) as conn:
            database = conn.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()
            started = conn.execute('SELECT pg_postmaster_start_time() AS started').fetchone()['started'].isoformat()
        if self.scenario['kind']=='ticket':
            status, health = self.service.transport.request('GET','/health',token=self.service.tokens['tenant-A'],request_id=label)
            assert status==200 and health==dict(status='ok',**self.service.identity)
            process = self.service.process
        else:
            health = self.service.management.request('GET','/api/v1/version')['body']
            assert health==dict(version=self.service.settings.version)
            process = self.gitea.process
        assert pg.process.poll() is None and process.poll() is None
        proof = dict(pg_pid=pg.process.pid, service_pid=process.pid, system_identifier=database['system_identifier'],
                     pg_started_at=started, health=health, at=datetime.now(timezone.utc).isoformat())
        save_json(self.directory/(label+'-services.json'),proof)
        return proof

    def __exit__(self,*args):
        try:
            save_json(self.directory/'final.json',self.observe())
        finally:
            self.service.cleanup(self.context,self.events)


def suite(output):
    directory = Path(output).resolve()/('d29-side-effects-'+uuid4().hex)
    directory.mkdir(parents=True)
    report = dict(status='RUNNING', summary_path=str(directory/'summary.json'), planned=len(scenarios()), scenarios=[],
                  model_requests=0, implementation_sha256=implementation_digest())
    save_json(directory/'summary.json',report)
    pg, gitea = PostgresRuntime(directory/'postgres'), GiteaRuntime(DEFAULT_INSTANCE_ROOT)
    agents, outages = [], []
    try:
        with pg, gitea, target_environment(gitea):
            store = Operations(pg.dsn)
            store.setup()
            report.update(postgres=pg.identity, gitea=gitea.identity, gitea_directory=str(gitea.directory))
            for scenario in scenarios():
                with Experiment(directory,store,scenario,gitea) as exp:
                    original = OperationAgent(exp,'crash')
                    agents.append(original)
                    barrier = original.barrier()
                    before = exp.capture('at-gate')
                    assert before['ledger']['state']==('confirmed' if scenario['window']=='after_checkpoint' else
                        'prepared' if scenario['window']=='before_call' else 'sent_unknown')
                    assert len(exp.targets(before['business']))==(0 if scenario['window'] in {'before_call','sent_unknown'} else 1)
                    first_alive = exp.alive(pg,'before-kill')
                    killed = original.kill(barrier)
                    after_alive = exp.alive(pg,'after-kill')
                    assert all(first_alive[k]==after_alive[k] for k in first_alive if k!='at')
                    exp.capture('after-kill')
                    if scenario['fault']=='duplicate':
                        response = exp.service.execution_client.request('POST', exp.service.repo_path+'/issues',
                            body=exp.manifest['side_effect']['request'],expected=201)
                        save_json(exp.directory/'duplicate-injection.json',response)
                    wait_expiry(store,exp.manifest)
                    exp.capture('before-recovery')
                    kwargs = {}
                    if scenario['fault']=='unavailable':
                        outage = Outage(exp.directory)
                        outages.append(outage)
                        kwargs['observation_origin'] = outage.origin
                    count = 3 if scenario['fault']=='concurrent' else 1
                    recovery_agents = [OperationAgent(exp,'recover',f'recover-{i}',**kwargs) for i in range(count)]
                    agents.extend(recovery_agents)
                    results = [agent.collect() for agent in recovery_agents]
                    assert any(r['result']['status']==scenario['expected'] for r in results), results
                    assert all(r['result']['status'] in {scenario['expected'],'BUSY'} for r in results), results
                    after = exp.capture('after-recovery')
                    assert len(exp.targets(after['business']))==scenario['count']
                    later = []
                    if scenario['fault']=='unavailable':
                        outage.close()
                        retry = OperationAgent(exp,'recover','read-restored')
                        agents.append(retry)
                        later.append(retry.collect())
                        assert later[-1]['result']['status']=='PASS', later
                    if scenario['expected']=='PASS' or scenario['fault']=='unavailable':
                        repeated = OperationAgent(exp,'recover','repeat-confirmed')
                        agents.append(repeated)
                        later.append(repeated.collect())
                        assert later[-1]['result']['status']=='PASS' and later[-1]['result']['cached']
                    final_alive = exp.alive(pg,'after-recovery')
                    assert all(first_alive[k]==final_alive[k] for k in first_alive if k!='at')
                    final = exp.capture('completed')
                    if scenario['name']=='ticket-query-1':
                        status, body = exp.service.transport.request('POST','/tickets',token=exp.service.tokens['tenant-A'],
                            request_id='different-content',operation_id=exp.context.operation_id,
                            body=exp.case['request']|dict(description='different content'))
                        assert status==409
                        save_json(exp.directory/'content-conflict.json',dict(http_status=status,body=body,request_sha256=digest(exp.case['request']|dict(description='different content'))))
                        assert exp.observe()==final['business']
                    if scenario['kind']=='ticket':
                        with sqlite3.connect((exp.directory/'business.sqlite').as_uri()+'?mode=ro',uri=True) as conn:
                            conn.row_factory=sqlite3.Row
                            mappings=[dict(row) for row in conn.execute('SELECT * FROM idempotency_keys')]
                        save_json(exp.directory/'idempotency-mappings.json',mappings)
                    record = dict(**scenario, run_dir=str(exp.directory), killed=killed, recovery=results, later=later,
                                  final_ledger=final['ledger']['state'], final_count=len(exp.targets(final['business'])))
                report['scenarios'].append(record)
                save_json(directory/'summary.json',report)
        report.update(status='PASS',executed=len(report['scenarios']),postgres_cleanup=pg.receipts,
                      gitea_cleanup=load_json(gitea.directory/'cleanup.json'))
    except Exception as exc:
        report.update(status='ERROR',error=type(exc).__name__+': '+str(exc))
    finally:
        for agent in agents:
            agent.close()
        for outage in outages:
            if outage.process.poll() is None:
                outage.close()
        save_json(directory/'summary.json',report)
    return report

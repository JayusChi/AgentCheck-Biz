"""Owned Agent process kills with PostgreSQL and business service kept alive."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from agentcheck_biz.adapters.ticket_http import TicketHttpEnvironment
from agentcheck_biz.adapters.http_transport import HttpTimeouts
from agentcheck_biz.checks import load_json, observe_database
from agentcheck_biz.reports import save_json
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.persistence.demo import Experiment
from agentcheck_biz.persistence.graph import build, config
from agentcheck_biz.persistence.runtime import PostgresRuntime, child_environment
from agentcheck_biz.persistence.store import digest, IDENTITY_KEYS
from agentcheck_biz.scheduling.store import Scheduler
from agentcheck_biz.scheduling.demo import wait_expiry
from .worker import snapshot, validate_spec


def scenarios():
    return [dict(name=f'commit-{strategy}-{repeat}', window='after_commit', strategy=strategy, repeat=repeat, damage='none')
        for repeat in range(1, 4) for strategy in ('replay', 'query_first')] + [
        dict(name='before-call', window='before_call', strategy='replay', repeat=1, damage='none'),
        dict(name='checkpoint-saved', window='after_checkpoint', strategy='replay', repeat=1, damage='none'),
        dict(name='checkpoint-missing', window='after_checkpoint', strategy='query_first', repeat=1, damage='missing'),
        dict(name='checkpoint-corrupt', window='after_checkpoint', strategy='query_first', repeat=1, damage='corrupt')]


class CrashExperiment(Experiment):
    def __init__(self, root, store, scenario):
        super().__init__(root, store)
        self.service = TicketHttpEnvironment(self.manifest['case'], 'unsafe', HttpTimeouts())
        self.spec = {k: self.manifest[k] for k in IDENTITY_KEYS} | scenario | dict(protocol='agent-crash/1',
            test_only=True, service_version='unsafe', model_requests=0,
            graph_source_sha256=hashlib.sha256((REPO_ROOT / 'agentcheck_biz/persistence/graph.py').read_bytes()).hexdigest(),
            prompt='deterministic prepare -> submit -> verify -> finish; no model', lease_seconds=3)
        validate_spec(self.spec, self.manifest)
        self.spec_path = self.directory / 'test-config.json'
        save_json(self.spec_path, self.spec)


class Agent:
    def __init__(self, exp, action):
        self.exp, self.action = exp, action
        self.output = exp.directory / action
        self.log = self.output.with_suffix('.log').open('wb')
        env = child_environment(dict(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1'))
        executable = sys.executable
        if os.name == 'nt':
            executable = sys._base_executable
            env['__PYVENV_LAUNCHER__'] = sys.executable
        self.process = subprocess.Popen([executable, '-X', 'utf8', '-m', 'agentcheck_biz.agent_crash.worker',
            '--manifest', str(exp.manifest_path), '--test-config', str(exp.spec_path), '--output', str(self.output),
            '--action', action], cwd=REPO_ROOT, env=env, stdin=subprocess.PIPE, stdout=self.log,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.process.stdin.write((json.dumps(dict(dsn=exp.store.dsn, origin=exp.service.transport.origin,
            token=exp.service.tokens['tenant-A'])) + '\n').encode())
        self.process.stdin.close()

    def barrier(self):
        deadline = time.monotonic() + 35
        while not (self.output / 'barrier.json').is_file():
            if self.process.poll() is not None:
                raise RuntimeError('Crash worker exited before its barrier: ' + str(self.output))
            if time.monotonic() > deadline:
                raise TimeoutError('Crash barrier deadline')
            time.sleep(.025)
        value = load_json(self.output / 'barrier.json')
        assert value['pid'] == self.process.pid and value['window'] == self.exp.spec['window']
        assert value['job_id'] == self.exp.manifest['job_id'] and self.process.poll() is None
        return value

    def kill(self, barrier):
        assert self.action == 'crash' and barrier['pid'] == self.process.pid and self.process.poll() is None
        started = datetime.now(timezone.utc).isoformat()
        self.process.kill()
        self.process.wait(timeout=5)
        self.log.close()
        receipt = dict(pid=self.process.pid, controller_pid=os.getpid(), exit_code=self.process.returncode,
            exited=self.process.poll() is not None, killed_at=started, observed_exit_at=datetime.now(timezone.utc).isoformat(),
            termination='owned_Popen.kill', barrier_sha256=digest(barrier), barrier_observed_alive=True,
            result_absent=not (self.output / 'result.json').exists())
        assert receipt['exit_code'] != 0 and receipt['result_absent']
        save_json(self.output / 'termination.json', receipt)
        return receipt

    def collect(self):
        self.process.wait(timeout=60)
        self.log.close()
        result = load_json(self.output / 'result.json')
        assert result['pid'] == self.process.pid
        receipt = dict(pid=self.process.pid, controller_pid=os.getpid(), exit_code=self.process.returncode,
                       exited=True, result=result)
        save_json(self.output / 'process.json', receipt)
        return receipt

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        self.log.close()


def services(exp, pg, label):
    assert pg.process.poll() is None and exp.service.process.poll() is None
    status, health = exp.service.transport.request('GET', '/health', token=exp.service.tokens['tenant-A'],
        request_id='controller-' + label)
    assert status == 200 and health == {'status': 'ok', **exp.service.identity}
    with exp.store.connect(readonly=True) as conn:
        database = conn.execute('SELECT system_identifier::text AS system_identifier FROM pg_control_system()').fetchone()
        uptime = conn.execute('SELECT pg_postmaster_start_time() AS started').fetchone()['started'].isoformat()
    assert database['system_identifier'] == pg.identity['system_identifier']
    result = dict(observed_at=datetime.now(timezone.utc).isoformat(), pg_pid=pg.process.pid,
        pg_system_identifier=database['system_identifier'], pg_started_at=uptime,
        http_pid=exp.service.process.pid, http_identity=health, health_request_id='controller-' + label)
    save_json(exp.directory / (label + '-services.json'), result)
    return result


def capture(exp, label):
    database = exp.store.export()
    save_json(exp.directory / (label + '-postgres.json'), database)
    value = snapshot(exp.store.dsn, exp.manifest, validate=False)
    payload = dict(checkpoint=value, sha256=digest(value), postgres_sha256=digest(database))
    save_json(exp.directory / (label + '-checkpoint.json'), payload)
    business = observe_database(exp.directory / 'business.sqlite')
    save_json(exp.directory / (label + '-business.json'), business)
    return value, business


def suite(output):
    directory = Path(output).resolve() / ('d28-agent-crash-' + uuid4().hex)
    directory.mkdir(parents=True)
    report = dict(status='RUNNING', summary_path=str(directory / 'summary.json'), model_requests=0,
        implementation_sha256=implementation_digest(), scenarios=[], planned=len(scenarios()))
    save_json(directory / 'summary.json', report)
    pg = PostgresRuntime(directory / 'postgres')
    agents = []
    try:
        with pg:
            store = Scheduler(pg.dsn)
            store.setup()
            report['postgres'] = pg.identity
            for scenario in scenarios():
                with CrashExperiment(directory, store, scenario) as exp:
                    original = Agent(exp, 'crash')
                    agents.append(original)
                    barrier = original.barrier()
                    at_gate, business = capture(exp, 'at-gate')
                    expected_phase = 'effect_observed' if scenario['window'] == 'after_checkpoint' else 'prepared'
                    assert at_gate['values']['phase'] == expected_phase and not at_gate['pending_errors']
                    effects = [t for t in business['tickets'] if t['tenant_id'] == 'tenant-A']
                    assert len(effects) == (0 if scenario['window'] == 'before_call' else 1)
                    if scenario['window'] == 'after_commit':
                        assert at_gate['values']['ticket'] is None and effects == [barrier['ticket']]
                    before = services(exp, pg, 'before-kill')
                    # Dedicated negative controls alter actual checkpoint storage,
                    # never the final result JSON or the business database.
                    if scenario['damage'] != 'none':
                        with store.saver(exp.manifest, barrier['lease']) as saver:
                            if scenario['damage'] == 'missing':
                                saver.delete_thread(exp.manifest['thread_id'])
                            else:
                                build(saver, exp.manifest).update_state(config(exp.manifest),
                                    dict(environment_id=str(uuid4())), as_node='submit')
                        save_json(exp.directory / 'damage.json', dict(kind=scenario['damage'], test_only=True,
                            original_checkpoint_id=at_gate['checkpoint_id'], storage='PostgresSaver', business_unchanged=True))
                        assert observe_database(exp.directory / 'business.sqlite') == business
                    killed = original.kill(barrier)
                    after = services(exp, pg, 'after-kill')
                    assert all(before[k] == after[k] for k in ('pg_pid', 'pg_system_identifier', 'pg_started_at', 'http_pid', 'http_identity'))
                    before_resume, _ = capture(exp, 'before-recovery')
                    save_json(exp.directory / 'expired.json', wait_expiry(store, exp.manifest))
                    recovery = Agent(exp, 'recover')
                    agents.append(recovery)
                    recovered = recovery.collect()
                    alive = services(exp, pg, 'after-recovery')
                    assert all(before[k] == alive[k] for k in ('pg_pid', 'pg_system_identifier', 'pg_started_at', 'http_pid', 'http_identity'))
                    final_checkpoint, final_business = capture(exp, 'after-recovery')
                    result = recovered['result']
                    assert result['lease']['generation'] == 2 and result['lease']['attempt_id'] != barrier['lease']['attempt_id']
                    assert recovered['pid'] != killed['pid'] and result['lease']['job_id'] == exp.manifest['job_id']
                    actual = [t for t in final_business['tickets'] if t['tenant_id'] == 'tenant-A']
                    expected_count = 2 if scenario['window'] == 'after_commit' and scenario['strategy'] == 'replay' else 1
                    assert len(actual) == expected_count
                    expected_outcome = 'ERROR' if scenario['damage'] != 'none' else 'FAIL' if expected_count == 2 else 'PASS'
                    assert result['status'] == ('ERROR' if expected_outcome == 'ERROR' else 'INCONCLUSIVE' if expected_outcome == 'FAIL' else 'PASS'), result
                    assert result['job_state'] == ('finished' if expected_outcome == 'PASS' else 'waiting_verification')
                    export = store.export()
                    # Each scenario preserves its own DB evidence at completion.
                    # Filter by thread/job later in the offline oracle.
                    save_json(exp.directory / 'postgres-snapshot.json', export)
                    record = dict(**scenario, run_dir=str(exp.directory), manifest=exp.manifest,
                        spec=exp.spec, expected=expected_outcome, resource_count=len(actual), killed=killed,
                        recovery=recovered, database_snapshot_sha256=digest(export))
                report['scenarios'].append(record)
                save_json(directory / 'summary.json', report)
            assert len(report['scenarios']) == report['planned']
        report.update(status='PASS', cleanup=pg.receipts, executed=len(report['scenarios']))
        assert all(item['exited'] for item in pg.receipts)
    except Exception as exc:
        report.update(status='ERROR', error=type(exc).__name__ + ': ' + str(exc))
    finally:
        for agent in agents:
            agent.close()
        save_json(directory / 'summary.json', report)
    return report

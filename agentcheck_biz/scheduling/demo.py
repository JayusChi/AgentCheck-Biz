"""Real competing processes, heartbeat loss, fenced zombie and verified takeover."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from agentcheck_biz.checks import load_json
from agentcheck_biz.reports import save_json
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.persistence.demo import Experiment
from agentcheck_biz.persistence.runtime import PostgresRuntime, child_environment
from agentcheck_biz.persistence.store import digest
from .store import Scheduler
from .worker import wait_file


class WorkerProcess:
    def __init__(self, experiment, label, action='run', seconds=15, *, barrier=None, release=None, lease=None):
        self.output = experiment.directory / label
        self.log = self.output.with_suffix('.log').open('wb')
        env = child_environment(dict(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1'))
        executable = sys.executable
        if os.name == 'nt':
            executable = sys._base_executable
            env['__PYVENV_LAUNCHER__'] = sys.executable
        args = [executable, '-X', 'utf8', '-m', 'agentcheck_biz.scheduling.worker', '--manifest', str(experiment.manifest_path),
                '--output', str(self.output), '--action', action, '--seconds', str(seconds)]
        for name, value in (('--barrier', barrier), ('--release', release)):
            if value is not None:
                args += [name, str(value)]
        self.process = subprocess.Popen(args, cwd=REPO_ROOT, env=env, stdin=subprocess.PIPE,
            stdout=self.log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        request = dict(dsn=experiment.store.dsn, origin=experiment.service.transport.origin,
                       token=experiment.service.tokens['tenant-A'], lease=lease)
        self.process.stdin.write((json.dumps(request) + '\n').encode())
        self.process.stdin.close()
        self.experiment, self.label = experiment, label
        self.collected = False

    def collect(self):
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.close()
            raise
        self.log.close()
        result = load_json(self.output / 'result.json')
        step = dict(label=self.label, pid=self.process.pid, parent_pid=os.getpid(), exited=True,
                    exit_code=self.process.returncode, result_path=str(self.output / 'result.json'), result=result)
        self.experiment.steps.append(step)
        save_json(self.experiment.directory / 'processes.json', self.experiment.steps)
        self.collected = True
        return step

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        self.log.close()


def wait_expiry(scheduler, manifest):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if scheduler.expire(manifest):
            row = scheduler.get(manifest)
            assert row['state'] == 'waiting_verification'
            return dict(state=row['state'], revision=row['revision'], attempt_id=str(row['attempt_id']))
        time.sleep(0.05)
    raise TimeoutError('Lease did not expire')


def suite(output):
    directory = Path(output).resolve() / ('d27-scheduler-' + uuid4().hex)
    directory.mkdir(parents=True)
    report = dict(status='RUNNING', model_requests=0, implementation_sha256=implementation_digest(),
                  assertions={}, summary_path=str(directory / 'summary.json'))
    processes = []
    def start(exp, label, *args, **kwargs):
        worker = WorkerProcess(exp, label, *args, **kwargs)
        processes.append(worker)
        return worker
    def call(exp, label, *args, **kwargs):
        return start(exp, label, *args, **kwargs).collect()
    def record(exp):
        return dict(run_dir=str(exp.directory), manifest=exp.manifest, steps=exp.steps)
    pg = PostgresRuntime(directory / 'postgres')
    try:
        with pg:
            scheduler = Scheduler(pg.dsn)
            scheduler.setup()
            scheduler.setup()
            report['postgres'] = pg.identity
            with Experiment(directory, scheduler) as race:
                gate = race.directory / 'claim.gate'
                # A separately launched heartbeat must finish cold Python/PG
                # imports before this claim expires. Keep real expiry below;
                # process startup latency is not the behavior under test here.
                workers = [start(race, 'racer-' + str(i), 'claim', 15, barrier=gate) for i in range(2)]
                for worker in workers:
                    wait_file(worker.output / 'ready.json')
                save_json(race.directory / 'barrier.json', dict(pids=[w.process.pid for w in workers],
                          both_alive=all(w.process.poll() is None for w in workers)))
                gate.write_text('release', encoding='utf8')
                steps = [w.collect() for w in workers]
                assert sorted(s['result']['status'] for s in steps) == ['BUSY', 'PASS'], steps
                winner = next(s for s in steps if s['result']['status'] == 'PASS')
                renewed = call(race, 'heartbeat', 'heartbeat', 15, lease=winner['result']['lease'])
                assert renewed['result']['status'] == 'PASS'
                denied = call(race, 'during-renewed-lease', 'claim')
                assert denied['result']['status'] == 'BUSY'
                report['assertions']['simultaneous_claim_has_one_owner_and_renewal_blocks_others'] = True
                save_json(race.directory / 'expired.json', wait_expiry(scheduler, race.manifest))
                empty = call(race, 'takeover')
                assert empty['result']['status'] == 'INCONCLUSIVE' and empty['result']['reconciliation']['checkpoint'] is None
                report['assertions']['expired_queue_without_checkpoint_queries_business_and_never_posts'] = True
            report['race'] = record(race)
            with Experiment(directory, scheduler) as zombie:
                gate = zombie.directory / 'stale.gate'
                old = start(zombie, 'old-worker', 'zombie', 3, release=gate)
                wait_file(old.output / 'paused.json')
                paused = load_json(old.output / 'paused.json')
                assert paused['status'] == 'PASS' and paused['values']['phase'] == 'effect_observed'
                save_json(zombie.directory / 'expired.json', wait_expiry(scheduler, zombie.manifest))
                resumed = call(zombie, 'takeover')
                assert resumed['result']['status'] == 'PASS' and resumed['result']['job_state'] == 'finished', resumed
                assert resumed['result']['lease']['generation'] == 2 and old.process.poll() is None
                gate.write_text('release', encoding='utf8')
                stale = old.collect()
                assert stale['result']['status'] == 'PASS' and stale['result']['stale_writes_unchanged'], stale
                assert set(stale['result']['probes'].values()) == {'REJECTED'}
                report['assertions']['same_old_process_status_renewal_and_checkpoint_writes_are_fenced'] = True
                report['assertions']['takeover_reads_checkpoint_and_business_before_safe_resume'] = True
            report['zombie'] = record(zombie)
            with Experiment(directory, scheduler) as gap:
                committed = call(gap, 'abandoned-gap', 'gap', 3)
                assert committed['result']['status'] == 'INCONCLUSIVE' and committed['result']['values']['phase'] == 'prepared'
                save_json(gap.directory / 'expired.json', wait_expiry(scheduler, gap.manifest))
                refused = call(gap, 'takeover')
                assert refused['result']['status'] == 'INCONCLUSIVE' and refused['result']['job_state'] == 'waiting_verification'
                assert len(refused['result']['reconciliation']['business_tickets']) == 1
                report['assertions']['commit_checkpoint_gap_remains_waiting_without_replaying_write'] = True
            report['gap'] = record(gap)
            export = scheduler.export()
            save_json(directory / 'postgres-snapshot.json', export)
            report['database_snapshot_sha256'] = digest(export)
        report['cleanup'] = pg.receipts
        assert all(r['exited'] for r in pg.receipts)
        report['status'] = 'PASS'
    except Exception as error:
        report.update(status='ERROR', error=type(error).__name__ + ': ' + str(error))
    finally:
        for process in processes:
            process.close()
        save_json(directory / 'summary.json', report)
    return report

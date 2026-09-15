"""Offline integration acceptance using owned Python workers, native PG and HTTP."""
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import time
from uuid import uuid4
from agentcheck_biz.checks import load_json
from agentcheck_biz.gitea_cases import issue_body,marker
from agentcheck_biz.persistence.store import new_manifest,digest
from agentcheck_biz.persistence.runtime import PostgresRuntime
from agentcheck_biz.persistence.graph import checkpointer,config,snapshot_payload
from agentcheck_biz.scheduling.demo import wait_expiry
from agentcheck_biz.side_effects.demo import Experiment as BaseExperiment,spawn,Outage
from agentcheck_biz.agent_crash.demo import Agent
from agentcheck_biz.reports import save_json
from agentcheck_biz.provenance import implementation_digest
from examples.gitea_target.runtime import GiteaRuntime,DEFAULT_INSTANCE_ROOT
from examples.gitea_target.demo import target_environment
from .store import Budgets
from .graph import graph
from .view import recovery_view,timestamp


def scenarios():
    # Budgets are fixed before the job is created, never adjusted by fault injection.
    base=dict(kind='ticket',window='after_model',action='crash',fault='none',model_limit=2,tool_limit=20,wall_seconds=180,expected='PASS',count=1)
    return [base|s for s in [
        dict(name='saved-tool-call'),
        dict(name='committed-tool-result',window='after_commit'),
        dict(name='concurrent-resume',window='after_commit',fault='concurrent'),
        dict(name='storage-unavailable',fault='storage'),
        dict(name='tool-budget-exhausted',window='after_commit',tool_limit=2,expected='INCONCLUSIVE'),
        dict(name='model-reservation-lost',window='model_reserved',model_limit=1,expected='INCONCLUSIVE',count=0),
        dict(name='final-model-budget',model_limit=1,expected='INCONCLUSIVE'),
        dict(name='absolute-deadline',fault='deadline',wall_seconds=15,expected='INCONCLUSIVE',count=0),
        dict(name='user-abort',action='hold',fault='abort',expected='INCONCLUSIVE',count=0),
        dict(name='late-result-after-abort',action='hold',window='after_commit',fault='abort',expected='INCONCLUSIVE'),
        dict(name='old-worker-return',action='zombie',window='after_commit',fault='stale'),
        dict(name='insufficient-read-evidence',window='after_commit',fault='unavailable',expected='INCONCLUSIVE'),
        dict(name='gitea-resume',kind='gitea',window='after_commit'),
        dict(name='gitea-unknown-empty',kind='gitea',window='sent_unknown',expected='INCONCLUSIVE',count=0),
    ]]


class Experiment(BaseExperiment):
    def __enter__(self):
        try:
            self.service.prepare(self.context,self.events)
            save_json(self.directory/'initial.json',self.observe())
            if self.scenario['kind']=='ticket':
                case=self.case
                op=dict(kind='ticket',scope=case['context']['tenant_id'],operation_id=self.context.operation_id,
                        request=case['request'],target=self.service.identity)
                connection=dict(origin=self.service.transport.origin,token=self.service.tokens['tenant-A'])
            else:
                case=dict(context=dict(operation_id=self.context.operation_id),request=self.case['request'])
                op=dict(kind='gitea',scope=self.service.repository['full_name'],operation_id=self.context.operation_id,
                    request=dict(title=self.case['request']['title'],body=issue_body(self.context,self.context.operation_id,self.case['request']['body'])),
                    target=self.service.settings.public(),repository=self.service.repository,marker=marker(self.context,self.context.operation_id))
                connection=dict(settings=asdict(self.service.settings))
            limits={k:self.scenario[k] for k in ('model_limit','tool_limit','wall_seconds')}
            self.manifest=new_manifest(self.directory,case)|dict(side_effect=op,recovery=limits|dict(version='continuity/1',client='offline-fixed/1'))
            self.manifest_path=self.directory/'manifest.json'
            save_json(self.manifest_path,self.manifest)
            self.store.create(self.manifest)
            self.spec=dict(test_only=True,job_id=self.manifest['job_id'],window=self.scenario['window'])
            self.spec_path=self.directory/'test-config.json'
            save_json(self.spec_path,self.spec)
            self.connection=connection|dict(dsn=self.store.dsn)
            return self
        except BaseException:
            self.service.cleanup(self.context,self.events)
            raise

    def capture(self,label):
        with checkpointer(self.store.dsn,readonly=True) as saver:
            state=graph(saver,self.manifest).get_state(config(self.manifest))
            checkpoint=snapshot_payload(state) if state.values else None
        data=dict(checkpoint=checkpoint,checkpoint_sha256=digest(checkpoint),ledger=self.store.read(self.manifest),
                  budget=self.store.budget(self.manifest),business=self.observe(),postgres=self.store.export())
        data['view']=recovery_view(self.manifest,data)
        save_json(self.directory/(label+'.json'),data)
        return data


class RecoveryAgent(Agent):
    def __init__(self,exp,action,label=None,**overrides):
        self.exp,self.action=exp,action
        self.output=exp.directory/(label or action)
        self.log=self.output.with_suffix('.log').open('wb')
        self.process=spawn('agentcheck_biz.continuity.worker',['--manifest',str(exp.manifest_path),'--output',str(self.output),
            '--action',action,'--test-config',str(exp.spec_path)],exp.connection|overrides,self.log)

    def release(self):
        save_json(self.output/'release.json',dict(test_only=True,job_id=self.exp.manifest['job_id'],pid=self.process.pid))


def suite(output, *, scenario_names=None):
    plans = scenarios()
    if scenario_names is not None:
        if (not scenario_names or len(set(scenario_names)) != len(scenario_names)
                or any(name not in {p['name'] for p in plans} for name in scenario_names)):
            raise ValueError('Unknown, empty or duplicate continuity selection')
        plans = [next(p for p in plans if p['name'] == name) for name in scenario_names]
    directory=Path(output).resolve()/('d30-continuity-'+uuid4().hex)
    directory.mkdir(parents=True)
    report=dict(status='RUNNING',summary_path=str(directory/'summary.json'),planned=len(plans),scenarios=[],
                model_requests=0,implementation_sha256=implementation_digest())
    save_json(directory/'summary.json',report)
    pg,gitea=PostgresRuntime(directory/'postgres'),GiteaRuntime(DEFAULT_INSTANCE_ROOT)
    agents,outages=[],[]
    try:
        with pg,gitea,target_environment(gitea):
            store=Budgets(pg.dsn)
            store.setup()
            report.update(postgres=pg.identity,gitea=gitea.identity,gitea_directory=str(gitea.directory))
            for plan in plans:
                print('scenario: '+plan['name'],flush=True)
                with Experiment(directory,store,plan,gitea) as exp:
                    original=RecoveryAgent(exp,plan['action'])
                    agents.append(original)
                    barrier=original.barrier()
                    at=exp.capture('at-gate')
                    before=exp.alive(pg,'before-interruption')
                    killed=None
                    if plan['action']=='crash':
                        killed=original.kill(barrier)
                    if plan['fault']=='abort':
                        assert store.abort(exp.manifest)
                        assert not store.abort(exp.manifest)
                        exp.capture('after-abort')
                        original.release()
                        assert original.collect()['result']['status']=='INCONCLUSIVE'
                    if plan['action']=='crash' or plan['fault']=='stale':
                        wait_expiry(store,exp.manifest)
                    if plan['fault']=='deadline':
                        end=timestamp(at['budget']['deadline']).timestamp()
                        while time.time()<=end:time.sleep(.05)
                    failures=[]
                    if plan['fault']=='storage':
                        pg.close()
                        failed=RecoveryAgent(exp,'recover','storage-down')
                        agents.append(failed)
                        failures.append(failed.collect())
                        assert failures[-1]['result']['status']=='ERROR'
                        assert not (failed.output/'dispatch.jsonl').exists()
                        pg.restart()
                        save_json(exp.directory/'storage-restart.json',dict(before=before,after=pg.identity,cleanup=pg.receipts))
                    exp.capture('before-recovery')
                    overrides={}
                    if plan['fault']=='unavailable':
                        outage=Outage(exp.directory)
                        outages.append(outage)
                        overrides['observation_origin']=outage.origin
                    count=3 if plan['fault']=='concurrent' else 1
                    workers=[RecoveryAgent(exp,'recover',f'recover-{i}',**overrides) for i in range(count)]
                    agents.extend(workers)
                    results=[w.collect() for w in workers]
                    assert any(r['result']['status']==plan['expected'] for r in results),results
                    assert all(r['result']['status'] in {plan['expected'],'BUSY'} for r in results),results
                    after=exp.capture('after-recovery')
                    assert len(exp.targets(after['business']))==plan['count']
                    if plan['fault']=='stale':
                        original.release()
                        late=original.collect()
                        save_json(exp.directory/'late-return.json',late)
                        stale=exp.capture('after-late-return')
                        assert late['result']['status']=='INCONCLUSIVE'
                        assert stale['checkpoint']==after['checkpoint'] and stale['ledger']==after['ledger']
                        assert stale['budget']['events']==after['budget']['events']
                    later=[]
                    if plan['fault']=='unavailable':
                        outage.close()
                        restored=RecoveryAgent(exp,'recover','read-restored')
                        agents.append(restored)
                        later.append(restored.collect())
                        assert later[-1]['result']['status']=='PASS'
                    repeat=RecoveryAgent(exp,'recover','repeat-resume')
                    agents.append(repeat)
                    later.append(repeat.collect())
                    assert later[-1]['result']['status']==('PASS' if plan['fault']=='unavailable' else plan['expected'])
                    final=exp.capture('completed')
                    assert len(exp.targets(final['business']))==plan['count']
                    assert final['budget']['deadline']==at['budget']['deadline']
                    exp.alive(pg,'after-recovery')
                    record=plan|dict(run_dir=str(exp.directory),killed=killed,recovery=results,later=later,storage_failures=failures,
                        final_count=len(exp.targets(final['business'])),view=final['view'],demonstration=after['view'])
                report['scenarios'].append(record)
                save_json(directory/'summary.json',report)
        report.update(status='PASS',executed=len(report['scenarios']),postgres_cleanup=pg.receipts,
                      gitea_cleanup=load_json(gitea.directory/'cleanup.json'))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        report.update(status='ERROR',error=type(exc).__name__+': '+str(exc))
    finally:
        for agent in agents:agent.close()
        for outage in outages:
            if outage.process.poll() is None:outage.close()
        save_json(directory/'summary.json',report)
    return report

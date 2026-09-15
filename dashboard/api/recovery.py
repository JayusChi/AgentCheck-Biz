"""Read-only, hash-checked D30 recovery evidence. No model or worker is started."""
import hashlib
from pathlib import Path
from fastapi import APIRouter,HTTPException
from agentcheck_biz.checks import load_json
from agentcheck_biz.continuity.view import recovery_view

ROOT=Path(__file__).resolve().parents[2]


class RecoveryEvidence:
    def __init__(self,report_path=ROOT/'docs/v2/d30-results.json',allowed_root=ROOT/'artifacts/v2'):
        self.report_path,self.allowed_root=Path(report_path),Path(allowed_root).resolve()

    def catalog(self):
        if not self.report_path.is_file():return None
        report=load_json(self.report_path)
        if report['status']!='PASS' or report['continuity_recheck']['status']!='PASS':
            raise ValueError('Recovery acceptance has not passed independent verification')
        directory=Path(report['continuity_suite']['summary_path']).resolve().parent
        if not directory.is_relative_to(self.allowed_root):raise ValueError('Foreign evidence root')
        hashes=report['continuity_evidence_sha256']
        def read(path):
            path=Path(path).resolve()
            if not path.is_relative_to(directory):raise ValueError('Foreign evidence path')
            relative=path.relative_to(directory).as_posix()
            if hashlib.sha256(path.read_bytes()).hexdigest()!=hashes.get(relative):raise ValueError('Evidence hash changed')
            return load_json(path)
        summary=read(directory/'summary.json')
        if summary['status']!='PASS':raise ValueError('Incomplete evidence')
        return summary,read

    def record(self,item,read,label):
        run=Path(item['run_dir'])
        m=read(run/'manifest.json')
        snapshot=read(run/(label+'.json'))
        view=recovery_view(m,snapshot)
        if snapshot['view']!=view:raise ValueError('Changed recovery view')
        return view|dict(name=item['name'],snapshot=label,evidence_at=snapshot['budget']['database_now']),snapshot

    def list(self):
        catalog=self.catalog()
        if catalog is None:return dict(ready=False,runs=[])
        summary,read=catalog
        return dict(ready=True,runs=[self.record(r,read,'after-recovery')[0] for r in summary['scenarios']])

    def detail(self,job_id,label):
        if label not in {'after-recovery','completed'}:raise HTTPException(422,'不支持的观察时间点')
        catalog=self.catalog()
        if catalog:
            summary,read=catalog
            for item in summary['scenarios']:
                view,snapshot=self.record(item,read,label)
                if view['job_id']==job_id:
                    return dict(view=view,budget_events=snapshot['budget']['events'],operation=snapshot['ledger'],
                        checkpoint=snapshot['checkpoint'],read_only=True)
        raise HTTPException(404,'恢复记录不存在')


def create_recovery_router(report_path=ROOT/'docs/v2/d30-results.json',allowed_root=ROOT/'artifacts/v2'):
    evidence=RecoveryEvidence(report_path,allowed_root)
    router=APIRouter(prefix='/api/business/recovery',tags=['recovery'])
    def checked(call):
        try:return call()
        except (OSError,ValueError,KeyError,TypeError,StopIteration) as exc:
            raise HTTPException(503,'恢复证据未通过完整性检查，暂不能显示业务成功。') from exc
    @router.get('/runs')
    def runs():return checked(evidence.list)
    @router.get('/runs/{job_id}')
    def detail(job_id:str,snapshot:str='after-recovery'):return checked(lambda:evidence.detail(job_id,snapshot))
    return router

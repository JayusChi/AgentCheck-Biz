"""D33 single verification command; see docs/v2/reproduce.md for pinned setup."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def clean_environment():
    keep = {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','COMSPEC','SYSTEMDRIVE','PATHEXT',
            'VIRTUAL_ENV','HOME','USERPROFILE','LOCALAPPDATA','APPDATA'}
    env = {k:v for k,v in os.environ.items() if k.upper() in keep}
    env.update(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',PYTHON_DOTENV_DISABLED='1',
               LANGCHAIN_TRACING_V2='false',LANGSMITH_TRACING='false',CI='true')
    return env


def stage_status(code, data):
    status = data.get('status')
    return status if status in {'PASS','FAIL','INCONCLUSIVE','ERROR'} and code == {'PASS':0,'FAIL':1,'INCONCLUSIVE':2,'ERROR':3}[status] else 'ERROR'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=['all','core','integration'], default='all')
    parser.add_argument('--output', help='New output directory; existing directories are refused')
    parser.add_argument('--container', action='store_true', help='Linux disposable container only')
    args = parser.parse_args()
    output = Path(args.output).resolve() if args.output else ROOT/'artifacts/ci'/('run-'+uuid4().hex)
    if output.exists():
        parser.error('Output already exists; choose a fresh path')
    if ROOT not in output.parents:
        parser.error('Output must be inside the source checkout')
    if os.name == 'nt':
        import ctypes
        if ctypes.windll.shell32.IsUserAnAdmin():
            from run_unprivileged import run
            return run([str(Path(__file__).resolve()),'--scope',args.scope,'--output',str(output)],
                       ROOT/'artifacts/ci'/('launcher-'+uuid4().hex+'.json'), 3600)
        from agentcheck_biz.v2.process import own_descendants
        own_descendants()
    elif not (args.container and Path('/.dockerenv').is_file() and os.getuid() != 0):
        parser.error('Linux integrations require the supplied non-root disposable container')
    from agentcheck_biz.ci.artifacts import publish, source_bundle, sha
    output.mkdir(parents=True)
    private = output/'private'
    private.mkdir()
    (ROOT/'tmp').mkdir(exist_ok=True)
    plan = ['configuration','dependencies']
    if args.scope in {'core','all'}:
        plan += ['core-tests','frontend-install','frontend-build']
    if args.scope in {'integration','all'}:
        plan += ['integration']
    plan += ['source-bundle']
    report = dict(scope=args.scope,status='INCONCLUSIVE',steps=[dict(name=n,status='INCONCLUSIVE') for n in plan])
    publish(report,output/'public')
    environment = clean_environment()
    try:
        for step in report['steps']:
            name = step['name']
            if name == 'source-bundle':
                bundle = source_bundle(ROOT, output/'agentcheck-v2-source.zip')
                report['bundle_sha256'] = bundle['sha256']
                report['source_sha256'] = bundle['source_sha256']
                step.update(status='PASS',exit_code=0)
            else:
                working = ROOT
                if name in {'configuration','core-tests','integration'}:
                    command = [sys.executable,'-X','utf8','-m','agentcheck_biz.ci.worker',name,
                               '--directory',str(private/name)]
                elif name == 'dependencies':
                    command = [sys.executable,'-m','pip','check']
                else:
                    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
                    if npm is None: raise RuntimeError('Pinned Node/npm installation required')
                    working = ROOT/'dashboard/frontend'
                    command = [npm, *(['ci','--ignore-scripts','--no-audit','--no-fund'] if name=='frontend-install' else ['run','build'])]
                with (private/(name+'.log')).open('wb') as log:
                    process = subprocess.Popen(command,cwd=working,env=environment,stdout=log,stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    try: code = process.wait(timeout=1800 if name=='integration' else 600)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=10)
                        code = 124
                result_path = private/name/'stage.json'
                data = json.loads(result_path.read_text(encoding='utf8')) if result_path.is_file() else {}
                status = (stage_status(code,data) if name in {'configuration','core-tests','integration'}
                          else 'PASS' if code == 0 else 'ERROR')
                step.update(status=status,exit_code=code)
                step.update({k:data[k] for k in ('tests','failures','errors','skipped') if k in data})
                if 'observed' in data: report['observed'] = data['observed']
            print(name+': '+step['status'],flush=True)
            publish(report,output/'public')
            # Configuration/dependency failures make subsequent execution unsafe.
            if name in {'configuration','dependencies'} and step['status'] != 'PASS': break
    except Exception:
        import traceback
        (private/'controller-error.log').write_text(traceback.format_exc(),encoding='utf8')
        step.update(status='ERROR',exit_code=3)
    order = {'PASS':0,'INCONCLUSIVE':1,'FAIL':2,'ERROR':3}
    report['status'] = max((s['status'] for s in report['steps']),key=order.get)
    public = publish(report,output/'public')
    print(json.dumps(dict(status=public['status'],scope=args.scope,output=str(output)),ensure_ascii=False),flush=True)
    return {'PASS':0,'FAIL':1,'INCONCLUSIVE':2,'ERROR':3}[public['status']]


if __name__ == '__main__':
    raise SystemExit(main())

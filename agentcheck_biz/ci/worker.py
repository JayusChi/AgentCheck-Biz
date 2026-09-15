"""Fixed stage entry points; private detailed evidence stays under the owned run."""
import argparse
import io
import json
import os
from pathlib import Path
import socket
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
CORE = [
    'tests.business',
    'tests.v2.test_regression.RegressionTests',
    'tests.v2.test_network_acceptance.PlanTests',
    'tests.v2.test_recovery_acceptance.ProtocolTests',
    'tests.v2.test_recovery_acceptance.BudgetTests',
    'tests.v2.test_ci',
    'tests.v2.test_delivery',
    'tests.v2.test_live_recovery',
]


def configuration():
    from agentcheck_biz.v2.manifest import build, catalog, validate
    from agentcheck_biz.persistence.store import PINS
    from importlib.metadata import version
    from packaging.requirements import Requirement
    value = json.loads((ROOT / 'ci/v2-contract.json').read_text(encoding='utf8'))
    if (set(value) != {'schema_version','ticket_candidate','network_profiles','recovery_scenarios','model_requests'}
        or value['schema_version'] != 'agentcheck-ci/1'
        or value['ticket_candidate'] not in {'C_fixed_loss_retry','B_unsafe_loss_retry'}
        or value['network_profiles'] != ['gitea_P00_transparent','Gitea_api_visible_loss']
        or value['recovery_scenarios'] != ['committed-tool-result','storage-unavailable','gitea-resume','gitea-unknown-empty']
        or type(value['model_requests']) is not int or value['model_requests'] != 0):
        raise ValueError('Unsupported CI contract')
    validate(build(list(catalog())), environment=False)
    if {key:version(key) for key in PINS} != PINS:
        raise ValueError('Dependency pins differ')
    for filename in ('requirements-acceptance.txt','requirements-persistence.txt'):
        for line in (ROOT/filename).read_text(encoding='utf8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'): continue
            requirement = Requirement(line)
            if requirement.marker is None or requirement.marker.evaluate():
                pins = list(requirement.specifier)
                if len(pins) != 1 or pins[0].operator != '==' or version(requirement.name) != pins[0].version:
                    raise ValueError('Frozen dependency differs: '+requirement.name)
    return value


def core():
    from .artifacts import diagnostic
    class Result(unittest.TextTestResult):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.diagnostics=[]
        def addError(self,test,err):
            self.diagnostics.append(diagnostic(err))
            super().addError(test,err)
        def addFailure(self,test,err):
            self.diagnostics.append(diagnostic(err))
            super().addFailure(test,err)
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT/'tests/business')))
    for name in CORE[1:]:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
    result = unittest.TextTestRunner(verbosity=2,resultclass=Result).run(suite)
    passed = result.testsRun > 0 and result.wasSuccessful() and not result.skipped
    return dict(status='PASS' if passed else 'FAIL', tests=result.testsRun,
                failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),diagnostics=result.diagnostics)


def integration(directory):
    from agentcheck_biz.network_acceptance.runner import execute, independent_check
    from agentcheck_biz.network_acceptance.profiles import profiles
    from agentcheck_biz.network_acceptance.verify import hashes, inspect_run
    from agentcheck_biz.network_acceptance.process import launch
    from agentcheck_biz.continuity.demo import suite
    config = configuration()
    known = {p['id']:p for p in profiles()}
    entries, observed = [], {}
    for index, name in enumerate([config['ticket_candidate'], *config['network_profiles']]):
        row = execute(known[name], directory/'network')
        raw = Path(row['run_dir'])
        result = inspect_run(raw, 'proxy')
        # The candidate always faces the FIXED business contract, including when
        # the deliberate red branch selects the real unsafe retry implementation.
        expected = known['C_fixed_loss_retry' if index == 0 else name]['expected']
        entries.append(dict(id='network-'+str(index),kind='proxy',run_dir=str(raw),
                            expected=expected,evidence_sha256=hashes(raw)))
        if index == 0:
            observed.update(ticket_count=result['resource_count'],ticket_tool_calls=result['tool_calls'])
    audit = independent_check(directory, entries)
    observed.update(network_planned=len(entries),network_checked=audit.get('executed',audit.get('planned',0)))
    recovery = suite(directory/'recovery', scenario_names=config['recovery_scenarios'])
    recovery_dir = Path(recovery['summary_path']).parent
    before = hashes(recovery_dir)
    checked = launch(['-m','agentcheck_biz.ci.worker','recovery-check','--evidence',str(recovery_dir),
                      '--directory',str(directory/'recovery-check')],
                     directory/'recovery-check.log',timeout=180)
    unchanged = before == hashes(recovery_dir)
    check_path = directory/'recovery-check/stage.json'
    verdict = json.loads(check_path.read_text(encoding='utf8')) if check_path.is_file() else {}
    observed.update(recovery_planned=len(config['recovery_scenarios']),recovery_checked=recovery.get('executed',0))
    if (recovery['status'] != 'PASS' or checked['exit_code'] != 0 or not unchanged
            or verdict.get('status') != 'PASS' or verdict.get('verifier_pid') != checked['pid']
            or verdict.get('experiments') != len(config['recovery_scenarios'])):
        status = 'ERROR'
    elif audit['status'] != 'PASS':
        status = 'FAIL'
    else:
        status = 'PASS'
    return dict(status=status, observed=observed)


def block_external(event, args):
    if event == 'socket.connect':
        address = args[1]
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1','::1','localhost'}:
            raise PermissionError('D33 stage permits loopback connections only')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['configuration','core-tests','integration','recovery-check'])
    parser.add_argument('--directory', required=True)
    parser.add_argument('--evidence')
    args = parser.parse_args()
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    sys.addaudithook(block_external)
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        if args.stage == 'configuration':
            configuration()
            result = dict(status='PASS')
        elif args.stage == 'core-tests':
            result = core()
        elif args.stage == 'recovery-check':
            from agentcheck_biz.continuity.verify import recheck
            from agentcheck_biz.network_acceptance.verify import ReadOnlyGuard
            config = configuration()
            sys.addaudithook(ReadOnlyGuard([Path(args.evidence)]))
            result = recheck(args.evidence,scenario_names=config['recovery_scenarios'])
        else:
            result = integration(directory)
    except Exception:
        import traceback
        from .artifacts import diagnostic
        detail = diagnostic(sys.exc_info())
        traceback.print_exc()
        result = dict(status='ERROR',diagnostics=[detail])
    (directory/'stage.json').write_text(json.dumps(result, indent=2),encoding='utf8')
    return {'PASS':0,'FAIL':1,'INCONCLUSIVE':2,'ERROR':3}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())

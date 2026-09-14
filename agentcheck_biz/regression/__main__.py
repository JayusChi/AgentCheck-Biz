"""Offline release comparison or explicitly separate harness self-test."""
import argparse
import json
import os
from pathlib import Path
import sys

from agentcheck_biz.checks import EXIT_CODES
from agentcheck_biz.network_acceptance.verify import ReadOnlyGuard, hashes
from agentcheck_biz.v2.manifest import read
from .compare import compare_batches, load_batch, policy
from .reports import export
from . import REPORT_VERSION


def output_directory(path,inputs):
    path=Path(path).resolve()
    roots=[Path(p).resolve() for p in inputs]
    if any(path==p or p in path.parents or path in p.parents for p in roots):
        raise ValueError('Report output must be outside all input evidence and manifest paths')
    path.mkdir(parents=True,exist_ok=False)
    return path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['compare','selftest'])
    parser.add_argument('--baseline-manifest',type=Path)
    parser.add_argument('--baseline',type=Path)
    parser.add_argument('--candidate-manifest',type=Path,required=True)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--policy',type=Path)
    parser.add_argument('--output',type=Path,required=True,help='New directory outside the inputs')
    args=parser.parse_args()
    output=None
    report=None
    try:
        inputs=[args.candidate_manifest,args.candidate]
        if args.command=='compare':
            if not args.baseline or not args.baseline_manifest:raise ValueError('Both baseline inputs are required')
            inputs += [args.baseline_manifest,args.baseline]
        elif args.baseline or args.baseline_manifest or args.policy:
            raise ValueError('Harness self-test cannot take baseline or release policy')
        if args.policy:inputs.append(args.policy)
        for root in inputs:
            if not root.exists():raise ValueError('Input does not exist: '+str(root))
            for p in [root,*root.rglob('*')] if root.is_dir() else [root]:
                if p.is_symlink() or getattr(p.lstat(),'st_file_attributes',0)&0x400:
                    raise ValueError('Linked input evidence is unsupported')
        output=output_directory(args.output,inputs)
        before={str(p):hashes(p) if p.is_dir() else p.read_bytes() for p in inputs}
        sys.dont_write_bytecode=True
        sys.addaudithook(ReadOnlyGuard(inputs))
        effective=policy(read(args.policy) if args.policy else None)
        baseline=load_batch(args.baseline_manifest,args.baseline) if args.command=='compare' else None
        candidate=load_batch(args.candidate_manifest,args.candidate)
        report=compare_batches(baseline,candidate,suite='release' if args.command=='compare' else 'harness',release_policy=effective)
        if before!={str(p):hashes(p) if p.is_dir() else p.read_bytes() for p in inputs}:
            raise ValueError('Read-only comparison changed input evidence')
        report.update(verifier_pid=os.getpid(),read_only=True,evidence_unchanged=True)
    except Exception as error:
        effective=policy()
        report=dict(schema_version=REPORT_VERSION,suite='release' if args.command=='compare' else 'harness',
            purpose='candidate_business_release' if args.command=='compare' else 'harness_selftest_only',
            status='ERROR',planned=0,slots=[],controls=[dict(status='ERROR',reason=type(error).__name__+': '+str(error))],
            policy=effective,policy_sha256=__import__('agentcheck_biz.persistence.store',fromlist=['digest']).digest(effective),model_requests=0)
    if output:
        try:export(report,output)
        except Exception as error:
            print(json.dumps(dict(status='ERROR',error='Report export failed: '+str(error)),ensure_ascii=False))
            return EXIT_CODES['ERROR']
    print(json.dumps(dict(status=report['status'],suite=report['suite'],planned=report['planned'],
        output=str(output) if output else None,controls=report['controls'],verifier_pid=os.getpid()),ensure_ascii=False))
    return EXIT_CODES[report['status']]


if __name__=='__main__':raise SystemExit(main())

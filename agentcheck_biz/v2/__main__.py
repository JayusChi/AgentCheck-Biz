"""python -m agentcheck_biz.v2 validate/run/batch/check --manifest frozen.json"""
import argparse
import json
import os
from pathlib import Path
import sys
from .manifest import read,validate,capabilities


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['validate','run','batch','check','capabilities','_worker'])
    parser.add_argument('--manifest',type=Path)
    parser.add_argument('--output',type=Path,help='Output parent for run/batch; existing batch for check')
    parser.add_argument('--slot')
    args=parser.parse_args()
    try:
        if args.command=='capabilities':result=capabilities()
        else:
            if not args.manifest:raise ValueError('--manifest is required')
            if args.command=='_worker':
                from .runner import worker
                result=worker(args.manifest,args.slot,args.output)
            else:
                manifest=read(args.manifest)
                validate(manifest,environment=args.command in {'validate','run','batch'},current_source=args.command!='check')
                if args.slot:raise ValueError('--slot is internal; run requires a one-slot manifest')
                if args.command=='validate':result=dict(status='PASS',slots=len(manifest['slots']),source_sha256=manifest['source_sha256'],resources_allocated=False)
                elif args.command=='check':
                    if not args.output:raise ValueError('--output must identify an existing batch')
                    from agentcheck_biz.network_acceptance.verify import ReadOnlyGuard,hashes
                    from .evidence import check
                    before=hashes(args.output)
                    sys.addaudithook(ReadOnlyGuard([args.output]))
                    result=check(args.output,manifest)
                    if hashes(args.output)!=before:raise ValueError('Read-only check changed evidence')
                    result.update(verifier_pid=os.getpid(),evidence_unchanged=True)
                else:
                    if not args.output:raise ValueError('--output is required')
                    if args.command=='run' and len(manifest['slots'])!=1:raise ValueError('run requires exactly one fixed slot; use batch')
                    from .runner import batch
                    result=batch(manifest,args.output)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        return 0 if result.get('status','PASS')=='PASS' else 2
    except Exception as error:
        print(json.dumps(dict(status='ERROR',error=type(error).__name__+': '+str(error),model_requests=0),ensure_ascii=False))
        return 3


if __name__=='__main__':raise SystemExit(main())

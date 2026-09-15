"""D34 preview by default; --live requires a current, output-bound authorization."""
import argparse
import json
from pathlib import Path
from agentcheck_biz.checks import load_json
from agentcheck_biz.persistence.store import digest
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from .plan import make_plan, validate, authorize


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path)
    parser.add_argument('--preview',action='store_true')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--offline-test',action='store_true')
    parser.add_argument('--retry-fixture',action='store_true')
    parser.add_argument('--authorization',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if sum((args.preview,args.live,args.offline_test))>1:parser.error('Choose one execution mode')
    plan=load_json(args.manifest) if args.manifest else make_plan()
    validate(plan)
    if not args.live and not args.offline_test:
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            save_json(args.output,plan)
        print(json.dumps(dict(mode='preview',model_requests=0,plan_sha256=digest(plan),plan=plan),ensure_ascii=False,indent=2))
        return 0
    if not args.output:parser.error('--output is required')
    record=None; key=None
    if args.live:
        if not args.authorization:parser.error('--authorization is required for live requests')
        record=load_json(args.authorization); authorize(plan,record,args.output)
        from dotenv import load_dotenv
        from pipeline.bailian import bailian_connection
        load_dotenv(REPO_ROOT/'.env',override=False)
        key,endpoint=bailian_connection()
        if endpoint.rstrip('/')!=plan['endpoint']:raise ValueError('Endpoint differs from approved preview')
    from .runner import batch
    result=batch(args.output,plan,'live' if args.live else 'offline-test',record,key,args.retry_fixture)
    print(json.dumps(dict(status=result['status'],counts=result['counts'],model_requests=result['model_requests'],output=str(args.output)),ensure_ascii=False))
    return dict(PASS=0,FAIL=1,INCONCLUSIVE=2,ERROR=3)[result['status']]


if __name__=='__main__':raise SystemExit(main())

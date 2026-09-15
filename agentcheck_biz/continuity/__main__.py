import argparse
import json
from .demo import suite


def main():
    parser=argparse.ArgumentParser(description='D30 offline recovery acceptance')
    parser.add_argument('--output')
    parser.add_argument('--check')
    args=parser.parse_args()
    if args.check:
        from .verify import recheck
        result=recheck(args.check)
    else:
        result=suite(args.output)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())

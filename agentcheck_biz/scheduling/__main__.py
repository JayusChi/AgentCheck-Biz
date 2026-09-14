import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='D27 real PostgreSQL lease and takeover acceptance')
    parser.add_argument('--output', type=Path, default=Path('artifacts/v2/scheduler'))
    parser.add_argument('--check', type=Path)
    args = parser.parse_args()
    if args.check:
        from .verify import recheck
        result = recheck(args.check)
    else:
        from .demo import suite
        result = suite(args.output)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

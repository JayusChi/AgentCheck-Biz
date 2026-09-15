import argparse
import json
from pathlib import Path
from .demo import suite


def main():
    parser = argparse.ArgumentParser(description="D26 real PostgreSQL checkpoint acceptance, no model calls")
    parser.add_argument("--output", type=Path, default=Path("artifacts/v2/checkpoint"))
    parser.add_argument("--check", type=Path, help="Read-only recheck of an exported D26 evidence directory")
    args = parser.parse_args()
    if args.check:
        from .verify import recheck
        result = recheck(args.check)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "PASS" else 1
    result = suite(args.output)
    print(json.dumps({k: result[k] for k in ("status", "summary_path", "assertions")}, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

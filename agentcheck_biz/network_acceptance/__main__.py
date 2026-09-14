"""Repeat all 20 core profiles five times, or independently recheck saved evidence."""

import argparse
import json
from pathlib import Path

from agentcheck_biz.checks import load_json
from agentcheck_biz.reports import save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/v2/network-acceptance"))
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--check", type=Path, help="Read-only recheck of a saved recheck-request.json in a new process")
    args = parser.parse_args()
    if args.worker:
        if not args.result:
            parser.error("--worker requires --result outside all evidence directories")
        from .verify import worker
        result = worker(load_json(args.worker))
        save_json(args.result, result)
        output = dict(status=result["status"], worker_pid=result["worker_pid"], planned=result["planned"], executed=result["executed"])
    elif args.check:
        from uuid import uuid4
        from .runner import independent_check
        directory = args.output.resolve() / ("recheck-" + uuid4().hex)
        directory.mkdir(parents=True)
        result = independent_check(directory, load_json(args.check)["entries"])
        output = dict(status=result["status"], result_path=str(directory / "independent-recheck.json"))
    else:
        from .runner import suite
        result = suite(args.output)
        output = {key: result[key] for key in ("status", "totals", "summary_path")}
    print(json.dumps(output, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

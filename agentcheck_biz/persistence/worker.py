"""One finite Agent process; credentials are supplied through stdin, never files."""

import argparse
import json
import os
from pathlib import Path
import sys

from agentcheck_biz.checks import load_json
from agentcheck_biz.reports import save_json
from .graph import execute, read_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--action", required=True, choices=("start", "read", "resume"))
    parser.add_argument("--gap", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    request = json.loads(sys.stdin.readline())
    try:
        manifest = load_json(args.manifest)
        if args.action == "read":
            result = dict(status="PASS", pid=os.getpid(), model_requests=0, **read_snapshot(request["dsn"], manifest))
        else:
            result = execute(request["dsn"], manifest, args.output, action=args.action,
                             origin=request["origin"], token=request["token"], gap=args.gap)
    except Exception as error:
        # Driver exceptions can contain connection strings. Persist the class only.
        result = dict(status="ERROR", pid=os.getpid(), model_requests=0, error_type=type(error).__name__,
                      reason="Checkpoint operation failed; no success or empty-state fallback")
    save_json(args.output / "result.json", result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 2 if result["status"] == "INCONCLUSIVE" else 3


if __name__ == "__main__":
    raise SystemExit(main())

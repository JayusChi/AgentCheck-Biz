"""Run the three D18 HTTP contracts; evidence is retained after child cleanup."""

import argparse
import json
from pathlib import Path

from agentcheck_biz.checks import load_json
from agentcheck_biz.lifecycle import run_business_case
from agentcheck_biz.provenance import REPO_ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-version", choices=("unsafe", "fixed"), default="fixed")
    parser.add_argument("--output", type=Path, default=Path("artifacts/v2/http"))
    args = parser.parse_args()
    results = [run_business_case(args.output, plugin_id="ticket-http",
               case=load_json(REPO_ROOT / f"cases/tickets/full/{case_id}.json"),
               options={"app_version": args.app_version}) for case_id in ("T01", "T02", "T09")]
    summary = {"mode": "scripted; real loopback HTTP; no model; no fault injection", "model_requests": 0,
               "app_version": args.app_version,
               "counts": {status: sum(item["result"]["status"] == status for item in results)
                          for status in ("PASS", "FAIL", "INCONCLUSIVE", "ERROR")},
               "runs": [{"case_id": item["run"]["case_id"], "status": item["result"]["status"],
                         "run_dir": item["run_dir"], "http_requests": item["run"]["http_request_attempts"],
                         "business_requests": item["run"]["http_business_request_attempts"],
                         "service": item["run"]["http_service"]} for item in results]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return max(item["result"]["exit_code"] for item in results)


if __name__ == "__main__":
    raise SystemExit(main())

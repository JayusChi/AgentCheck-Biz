"""Reproduce D23 without model credentials or provider requests."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from agentcheck_biz.reports import save_json
from .ticket_http import run_case, recheck


def suite(output):
    directory = Path(output).resolve() / ("d23-recovery-" + uuid4().hex)
    specs = [
        ("control", {"fault": "none"}, ("PASS", "PASS", 1, 1)),
        ("legacy_retry", {"verify_first": False}, ("FAIL", "FAIL", 2, 2)),
        ("verify_first", {}, ("PASS", "PASS", 1, 2)),
        ("not_forwarded", {"fault": "reject"}, ("PASS", "PASS", 1, 2)),
        ("permanent_rejection", {"fault": "permanent"}, ("PASS", "PASS", 0, 1)),
        ("budget_exhausted", {"max_tool_calls": 1}, ("PASS", "INCONCLUSIVE", 1, 1)),
        ("same_key_after_empty_query", {"fault": "unqualified_reject", "app_version": "fixed"}, ("PASS", "PASS", 1, 3)),
        ("empty_query_without_idempotency", {"fault": "unqualified_reject"}, ("PASS", "INCONCLUSIVE", 0, 2)),
    ]
    runs = []
    for label, options, expected in specs:
        item = run_case(directory, **options)
        actual = tuple(item[k] for k in ("policy_status", "business_status", "resource_count", "tool_calls"))
        runs.append({"label": label, **item, "expected": list(expected), "accepted": actual == expected})
    result = {"status": "PASS" if all(r["accepted"] for r in runs) else "FAIL", "model_requests": 0,
              "runs": runs, "summary_path": str(directory / "summary.json")}
    save_json(directory / "summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/v2/recovery"))
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    result = recheck(args.check) if args.check else suite(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status", result.get("policy_status")) == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

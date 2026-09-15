"""Run D24's three crash points and one shared-budget negative scenario."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from agentcheck_biz.reports import save_json
from . import POINTS
from .runner import run_case
from .verify import recheck


def suite(output):
    directory = Path(output).resolve() / ("d24-crash-" + uuid4().hex)
    specs = [(p, 6) for p in POINTS] + [("after_commit", 1)]
    runs = []
    for point, maximum in specs:
        item = run_case(directory, point, max_tool_calls=maximum)
        expected_calls = 1 if maximum == 1 else 3 if point == "before_commit" else 2
        expected_business = "INCONCLUSIVE" if maximum == 1 else "PASS"
        accepted = (item["status"] == "PASS" and item["coverage"] == "covered" and item["resource_count"] == 1
                    and item["tool_calls"] == expected_calls and item["business_status"] == expected_business)
        runs.append({**item, "max_tool_calls": maximum, "accepted": accepted})
    result = {"status": "PASS" if all(r["accepted"] for r in runs) else "FAIL", "model_requests": 0,
              "planned": len(specs), "covered": sum(r["coverage"] == "covered" for r in runs),
              "runs": runs, "summary_path": str(directory / "summary.json")}
    save_json(directory / "summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/v2/service-crash"))
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    result = recheck(args.check) if args.check else suite(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

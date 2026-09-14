"""D21 proxy transport contracts; fault coverage is separate from business status."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from agentcheck_biz.checks import load_json
from agentcheck_biz.fault_proxy.verify import assess_proxy_evidence
from agentcheck_biz.lifecycle import run_business_case
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from examples.gitea_target.demo import target_environment
from examples.gitea_target.runtime import DEFAULT_INSTANCE_ROOT, GiteaRuntime


def run_proxy_case(output, *, plugin_id, case, proxy, options=None):
    item = run_business_case(output, plugin_id=plugin_id, case=case, options={**(options or {}), "proxy": proxy})
    item["proxy_contract"] = assess_proxy_evidence(item["run_dir"])
    save_json(Path(item["run_dir"]) / "proxy-checks.json", item["proxy_contract"])
    report = Path(item["run_dir"]) / "report.md"
    with report.open("a", encoding="utf-8") as stream:
        stream.write(f"\n代理契约：{item['proxy_contract']['status']}；覆盖：{item['proxy_contract']['coverage']}。"
                     "[详细传输检查](proxy-checks.json)。\n")
    return item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/v2/proxy"))
    parser.add_argument("--check", type=Path, help="Read-only recheck of saved proxy coverage")
    args = parser.parse_args()
    if args.check:
        result = assess_proxy_evidence(args.check)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result["exit_code"]
    directory = args.output.resolve() / ("proxy-demo-" + uuid4().hex)
    directory.mkdir(parents=True)
    results = []

    def run_object(plugin, case_path, tool):
        for profile in sorted((REPO_ROOT / "cases/proxy").glob("P*.json")):
            scenario = load_json(profile)
            if scenario["rule"]:
                scenario["rule"]["tool"] = tool
            item = run_proxy_case(directory / "runs", plugin_id=plugin, case=load_json(REPO_ROOT / case_path), proxy={"rule": scenario["rule"]})
            results.append({"scenario_id": scenario["scenario_id"], "plugin": plugin, "run_dir": item["run_dir"],
                "business_status": item["result"]["status"], "coverage_status": item["proxy_contract"]["status"],
                "coverage": item["proxy_contract"]["coverage"], "reason": item["proxy_contract"]["reason"]})

    run_object("ticket-mcp", "cases/tickets/full/T01.json", "create_ticket")
    runtime = GiteaRuntime(DEFAULT_INSTANCE_ROOT)
    try:
        with runtime, target_environment(runtime):
            run_object("gitea-mcp", "cases/gitea/G01.json", "create_issue")
        expected = {"P00_transparent": ("PASS", "PASS", "transparent"), "P01_reject": ("ERROR", "PASS", "covered"),
                    "P02_delay": ("ERROR", "PASS", "covered"), "P03_drop": ("ERROR", "PASS", "covered"),
                    "P04_miss": ("INCONCLUSIVE", "INCONCLUSIVE", "not_covered")}
        passed = len(results) == 10 and all((r["business_status"], r["coverage_status"], r["coverage"]) == expected[r["scenario_id"]] for r in results)
        summary = {"status": "PASS" if passed else "ERROR", "model_requests": 0, "runs": results,
                   "business_counts": {s: sum(r["business_status"] == s for r in results) for s in ("PASS", "FAIL", "INCONCLUSIVE", "ERROR")},
                   "coverage_counts": {s: sum(r["coverage_status"] == s for r in results) for s in ("PASS", "FAIL", "INCONCLUSIVE", "ERROR")},
                   "instance": runtime.identity, "instance_directory": str(runtime.directory),
                   "scope": "D21 transport mechanisms; no commit barrier or recovery policy"}
        save_json(directory / "summary.json", summary)
        print(json.dumps({**summary, "summary_path": str(directory / "summary.json")}, ensure_ascii=False, indent=2))
        return 0 if passed else 3
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())

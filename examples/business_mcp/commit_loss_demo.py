"""D22 A/B/C ticket comparison and Gitea API-visible response-loss evidence."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from agentcheck_biz.checks import load_json
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from agentcheck_biz.fault_proxy.verify import assess_proxy_evidence
from examples.business_mcp.proxy_demo import run_proxy_case
from examples.gitea_target.demo import target_environment
from examples.gitea_target.runtime import GiteaRuntime, DEFAULT_INSTANCE_ROOT


def ticket_case(output, version, fault=True):
    return run_proxy_case(output, plugin_id="ticket-mcp", case=load_json(REPO_ROOT / "cases/tickets/full/T01.json"),
        proxy={"rule": load_json(REPO_ROOT / "cases/commit_loss/ticket.json") if fault else None},
        options={"commit_loss": True, "app_version": version})


def gitea_case(output):
    return run_proxy_case(output, plugin_id="gitea-mcp", case=load_json(REPO_ROOT / "cases/gitea/G01.json"),
        proxy={"rule": load_json(REPO_ROOT / "cases/commit_loss/gitea.json")}, options={"commit_loss": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/v2/commit-loss"))
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    if args.check:
        result = assess_proxy_evidence(args.check)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result["exit_code"]
    directory = args.output.resolve() / ("commit-loss-" + uuid4().hex)
    runs = []

    def capture(label, item, expected_business, expected_coverage):
        run = item["run"]
        final = load_json(Path(item["run_dir"]) / "final.json")
        resources = final.get("tickets", final.get("issues", []))
        if "tickets" in final:
            resources = [r for r in resources if r["tenant_id"] == "tenant-A" and r["operation_id"] == run["context"]["operation_id"]]
        runs.append({"label": label, "run_dir": item["run_dir"], "business_status": item["result"]["status"],
            "coverage_status": item["proxy_contract"]["status"], "coverage": item["proxy_contract"]["coverage"],
            "tool_calls": run["tool_calls"], "resource_count": len(resources), "expected_business": expected_business,
            "expected_coverage": expected_coverage, "reason": item["result"]["reason"],
            "coverage_reason": item["proxy_contract"]["reason"]})

    capture("A_unsafe_control", ticket_case(directory / "runs", "unsafe", False), "PASS", "transparent")
    capture("B_unsafe_loss_retry", ticket_case(directory / "runs", "unsafe"), "FAIL", "covered")
    capture("C_fixed_loss_retry", ticket_case(directory / "runs", "fixed"), "PASS", "covered")
    runtime = GiteaRuntime(DEFAULT_INSTANCE_ROOT)
    with runtime, target_environment(runtime):
        capture("Gitea_api_visible_loss", gitea_case(directory / "runs"), "ERROR", "covered")
    passed = all(r["business_status"] == r["expected_business"] and r["coverage_status"] == "PASS"
                 and r["coverage"] == r["expected_coverage"] for r in runs)
    summary = {"status": "PASS" if passed else "ERROR", "runs": runs, "model_requests": 0,
               "instance_directory": str(runtime.directory), "instance": runtime.identity,
               "fault_coverage": {"planned": 3, "covered": sum(r["coverage_status"] == "PASS" and r["coverage"] == "covered" for r in runs),
                                  "transparent_controls": 1},
               "scope": "Ticket A/B/C same fault/budget; Gitea API visibility only, no recovery"}
    save_json(directory / "summary.json", summary)
    print(json.dumps({**summary, "summary_path": str(directory / "summary.json")}, ensure_ascii=False, indent=2))
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())

"""D20 two-object compatibility run, including expected duplicate failures."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from agentcheck_biz.checks import load_json
from agentcheck_biz.lifecycle import run_business_case
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from agentcheck_biz.verifiers.ticket_local import recheck_ticket_run
from agentcheck_biz.verifiers.gitea import recheck_gitea_run
from examples.gitea_target.demo import target_environment
from examples.gitea_target.runtime import DEFAULT_INSTANCE_ROOT, GiteaRuntime
from examples.business_mcp import SDK_VERSION, PROTOCOL_VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/v2/mcp"))
    parser.add_argument("--check", type=Path, help="Recheck saved evidence without starting any process or network connection")
    args = parser.parse_args()
    if args.check:
        plugin = load_json(args.check / "run.json")["plugin"]["name"]
        verifier = {"ticket-mcp": recheck_ticket_run, "gitea-mcp": recheck_gitea_run}[plugin]
        result = verifier(args.check)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result["exit_code"]
    directory = args.output.resolve() / ("mcp-demo-" + uuid4().hex)
    directory.mkdir(parents=True)
    results = []

    def run(plugin, case_path, options=None):
        item = run_business_case(directory / "runs", plugin_id=plugin, case=load_json(REPO_ROOT / case_path), options=options)
        results.append({"plugin": plugin, "case_id": item["run"]["case_id"], "app_version": item["run"]["app_version"],
            "status": item["result"]["status"], "reason": item["result"]["reason"], "run_dir": item["run_dir"]})

    for version in ("fixed", "unsafe"):
        for case_id in ("T01", "T02", "T09"):
            run("ticket-mcp", f"cases/tickets/full/{case_id}.json", {"app_version": version})
    runtime = GiteaRuntime(DEFAULT_INSTANCE_ROOT)
    try:
        with runtime, target_environment(runtime):
            for case_id in ("G01", "G02", "G03"):
                run("gitea-mcp", f"cases/gitea/{case_id}.json")
        summary = {"mode": "scripted; real MCP stdio; independent object observers; no fault injection", "model_requests": 0,
            "sdk_version": SDK_VERSION, "protocol_version": PROTOCOL_VERSION,
            "instance": runtime.identity, "instance_directory": str(runtime.directory), "runs": results,
            "counts": {status: sum(row["status"] == status for row in results) for status in ("PASS", "FAIL", "INCONCLUSIVE", "ERROR")}}
        save_json(directory / "summary.json", summary)
        print(json.dumps({**summary, "summary_path": str(directory / "summary.json")}, ensure_ascii=False, indent=2))
        return 3 if summary["counts"]["ERROR"] else 2 if summary["counts"]["INCONCLUSIVE"] else 1 if summary["counts"]["FAIL"] else 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())

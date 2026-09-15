"""Real official Gitea evidence with scoped environment credentials; no model."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
from uuid import uuid4

from agentcheck_biz.checks import load_json
from agentcheck_biz.lifecycle import run_business_case
from agentcheck_biz.provenance import REPO_ROOT
from agentcheck_biz.reports import save_json
from agentcheck_biz.verifiers.gitea import recheck_gitea_run
from .runtime import DEFAULT_INSTANCE_ROOT, GiteaRuntime


@contextmanager
def target_environment(runtime):
    values = runtime.environment()
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/v2/gitea"))
    parser.add_argument("--check", type=Path, help="Only recheck saved API observations, with no server/network")
    args = parser.parse_args()
    if args.check:
        result = recheck_gitea_run(args.check)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result["exit_code"]
    directory = args.output.resolve() / ("gitea-demo-" + uuid4().hex)
    directory.mkdir(parents=True)
    results = []
    # Gitea launches native Git with its repository as cwd. Windows cannot
    # reliably create that process under a deeply nested evidence directory.
    runtime = GiteaRuntime(DEFAULT_INSTANCE_ROOT)
    try:
        with runtime, target_environment(runtime):
            for case_id in ("G01", "G02", "G03"):
                outcome = run_business_case(directory / "runs", plugin_id="gitea",
                                            case=load_json(REPO_ROOT / f"cases/gitea/{case_id}.json"))
                results.append({"case_id": outcome["run"]["case_id"], "status": outcome["result"]["status"],
                                "reason": outcome["result"]["reason"], "run_dir": outcome["run_dir"]})
        summary = {"instance": runtime.identity, "instance_directory": str(runtime.directory), "model_requests": 0,
                   "mode": "official Gitea HTTP; independent read-only API; no fault injection", "runs": results,
                   "counts": {status: sum(row["status"] == status for row in results) for status in ("PASS", "FAIL", "INCONCLUSIVE", "ERROR")}}
        save_json(directory / "summary.json", summary)
        print(json.dumps({**summary, "summary_path": str(directory / "summary.json")}, ensure_ascii=False, indent=2))
        return 3 if summary["counts"]["ERROR"] else 2 if summary["counts"]["INCONCLUSIVE"] else 1 if summary["counts"]["FAIL"] else 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())

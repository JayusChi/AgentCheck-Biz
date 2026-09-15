"""Explicit, budget-limited live run using the existing Bailian credentials."""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from agentcheck_biz.runner import REPO_ROOT, default_case, run_ticket_case
from .llm_agent import ModelConfig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["clean", "faulted"], default="faulted")
    parser.add_argument("--app-version", choices=["unsafe", "fixed"], default="fixed")
    parser.add_argument("--model", help="Defaults to AGENTCHECK_BIZ_MODEL or the D1-verified qwen3.7-max")
    args = parser.parse_args()
    load_dotenv(REPO_ROOT / ".env", override=False)
    model = args.model or os.getenv("AGENTCHECK_BIZ_MODEL", "qwen3.7-max")
    config = ModelConfig(model=model)
    case = default_case()
    case["case_id"] = "D5_live_create_ticket"
    case["limits"] = {"max_tool_calls": 6, "max_model_calls": config.max_model_calls,
                      "model_loop_timeout_seconds": config.timeout_seconds,
                      "max_output_tokens_per_model_call": config.max_output_tokens}
    outcome = run_ticket_case(
        REPO_ROOT / "artifacts", app_version=args.app_version, inject_fault=args.phase == "faulted",
        case=case, agent="llm", model_config=config,
    )
    run, result = outcome["run"], outcome["result"]
    print(f"D5 {args.phase}: verdict={result['status']}, model_called={run['model_called']}, "
          f"tool_calls={run['tool_calls']}")
    print(result["reason"])
    print(f"Model responses: {run['model']['model_responses']}; usage: {run['model']['usage']}")
    print(f"Report: {Path(outcome['run_dir']) / 'report.md'}")
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())

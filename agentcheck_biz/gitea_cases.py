"""Versioned Issue contracts, independent of ticket fields and schema."""

from copy import deepcopy
import json

import jsonschema

from .provenance import REPO_ROOT


def validate_gitea_case(case):
    schema = json.loads((REPO_ROOT / "schema/gitea-case.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(case)
    result = deepcopy(case)
    if any(row["operation_id"] == result["operation_id"] for row in result["initial_issues"]):
        raise ValueError("Gitea initial issues must be unrelated operations")
    for row in [result["request"], *result["initial_issues"]]:
        if not row["title"].strip() or "<!-- agentcheck:" in row["body"]:
            raise ValueError("Issue fields cannot inject the harness operation marker")
    return result


def issue_body(context, operation_id, text):
    return text + f"\n\n<!-- agentcheck:run={context.run_id};operation={operation_id} -->"


def marker(context, operation_id):
    return f"<!-- agentcheck:run={context.run_id};operation={operation_id} -->"

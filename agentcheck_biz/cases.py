"""Strict JSON cases: no unknown options, duplicate keys or executable rules."""

from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator


class CaseValidationError(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CaseValidationError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise CaseValidationError(f"Non-finite JSON number is not allowed: {value}")


def validate_case(case: dict) -> dict:
    # Serializing also rejects non-finite values supplied through the Python API.
    try:
        json.dumps(case, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise CaseValidationError(str(error)) from error
    schema_path = Path(__file__).resolve().parents[1] / "schema" / "business-case.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(case))
    if errors:
        details = [f"{'.'.join(map(str, error.absolute_path)) or '$'}: {error.message}" for error in errors[:5]]
        raise CaseValidationError("; ".join(details))
    ids = [row["ticket_id"] for row in case["initial_tickets"]]
    if len(ids) != len(set(ids)):
        raise CaseValidationError("initial_tickets contains duplicate ticket_id values")
    expected, fault = case["expected"], case["fault"]
    if expected["client_status"] == "completed" and expected["ticket_count_for_operation"] != 1:
        raise CaseValidationError("completed requires one supported ticket")
    if fault and fault.get("kind") == "F3":
        if (expected["ticket_count_for_operation"], expected["client_status"], expected.get("client_reason")) != (
            0, "blocked", "permission_denied"
        ):
            raise CaseValidationError("F3 requires zero tickets, blocked status and permission_denied reason")
    if fault and fault.get("kind") == "F2" and fault.get("max_injections") == 1:
        if (expected["ticket_count_for_operation"], expected["client_status"]) != (1, "completed"):
            raise CaseValidationError("F2 expects recovery with one ticket and completed status")
    scenario = case.get("scenario", "retry")
    context = case["context"]
    secondary = case.get("secondary_context")
    if scenario in {"tenant_isolation", "distinct_operations"}:
        if secondary is None:
            raise CaseValidationError("Two-operation scenarios require secondary_context")
        if scenario == "tenant_isolation" and (secondary["tenant_id"] == context["tenant_id"] or secondary["operation_id"] != context["operation_id"]):
            raise CaseValidationError("Tenant isolation requires different tenants and the same local operation ID")
        if scenario == "distinct_operations" and (secondary["tenant_id"] != context["tenant_id"] or secondary["operation_id"] == context["operation_id"]):
            raise CaseValidationError("Distinct operations require the same tenant and different operation IDs")
    elif secondary is not None:
        raise CaseValidationError("secondary_context requires a two-operation scenario")
    prior = [row for row in case["initial_tickets"] if all(row[k] == v for k, v in context.items())]
    if scenario in {"replay", "conflict"}:
        if len(prior) != 1:
            raise CaseValidationError("Replay/conflict requires exactly one prior target ticket")
        same = all(prior[0][k] == v for k, v in case["request"].items())
        if same != (scenario == "replay"):
            raise CaseValidationError("Initial payload does not match the replay/conflict scenario")
    if scenario == "concurrent" and fault is not None:
        raise CaseValidationError("Concurrency contract does not inject faults")
    if scenario == "query_after_unknown" and (not fault or fault.get("kind", "F1") != "F1"):
        raise CaseValidationError("Query-after-unknown requires F1")
    if fault and fault.get("kind") == "missing_id" and scenario != "malformed_result":
        raise CaseValidationError("missing_id requires malformed_result scenario")
    if scenario == "malformed_result" and (not fault or fault.get("kind") != "missing_id"):
        raise CaseValidationError("Malformed-result scenario requires missing_id fault")
    if expected.get("returned_id_policy") == "null" and expected["client_status"] == "completed":
        raise CaseValidationError("Completed must return a supported ID")
    if fault and fault.get("kind") == "F2" and fault.get("max_injections") is None:
        if fault["occurrence"] != 1 or (expected["ticket_count_for_operation"], expected["client_status"], expected.get("client_reason")) != (0, "needs_verification", "temporarily_unavailable"):
            raise CaseValidationError("Persistent F2 requires no writes and explicit unavailable result")
    return deepcopy(case)


def load_case(path: Path) -> dict:
    try:
        case = json.loads(Path(path).read_text(encoding="utf-8-sig"),
                          object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        return validate_case(case)
    except (OSError, ValueError) as error:
        raise CaseValidationError(f"{path}: {error}") from error


def load_suite(directory: Path) -> list[tuple[Path, dict]]:
    if not Path(directory).is_dir():
        raise CaseValidationError(f"Case directory does not exist: {directory}")
    paths = sorted(Path(directory).glob("*.json"))
    if not paths:
        raise CaseValidationError("Case directory contains no JSON files")
    cases = [(path, load_case(path)) for path in paths]
    ids = [case["case_id"] for _, case in cases]
    if len(ids) != len(set(ids)):
        raise CaseValidationError("Suite contains duplicate case_id values")
    return cases

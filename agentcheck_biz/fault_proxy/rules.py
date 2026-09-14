"""Validate versioned rules before resource allocation; resolve only run binding."""

from copy import deepcopy
import json
from urllib.parse import urlsplit

import jsonschema

from agentcheck_biz.adapters.contracts import PluginConfigurationError
from agentcheck_biz.provenance import REPO_ROOT


def validate_options(options):
    if not isinstance(options, dict) or set(options) != {"rule"}:
        raise PluginConfigurationError("Proxy options must contain only rule (null means transparent)")
    return {"rule": validate_rule(options["rule"])}


def validate_rule(rule):
    if rule is None:
        return None
    try:
        filename = "commit-loss-rule.schema.json" if isinstance(rule, dict) and rule.get("schema_version") == 2 else "proxy-rule.schema.json"
        schema = json.loads((REPO_ROOT / "schema" / filename).read_text(encoding="utf-8"))
        jsonschema.validate(rule, schema)
        if rule["max_triggers"] > len(rule["request_numbers"]):
            raise ValueError("Trigger cap exceeds selected request numbers")
        return deepcopy(rule)
    except (jsonschema.ValidationError, ValueError) as error:
        raise PluginConfigurationError("Invalid network fault rule") from error


def resolve_rule(rule, run_id):
    result = validate_rule(rule)
    if result is not None and result["run_id"] == "$run_id":
        result["run_id"] = run_id
    return result


def loopback(origin):
    address = urlsplit(origin)
    if (address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port
            or address.username or address.password or address.path or address.query or address.fragment):
        raise PluginConfigurationError("Proxy upstream must be the allocated loopback test origin")
    return address


def matches(rule, run_id, tool, number, hits):
    return bool(rule and rule["run_id"] == run_id and rule["tool"] == tool
                and number in rule["request_numbers"] and hits < rule["max_triggers"])

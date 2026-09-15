"""D23 entry adapter: real HTTP with existing D21/D22 owned fault processes."""

from dataclasses import replace
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx

from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.adapters.http_transport import HttpTimeouts, HttpTransport
from agentcheck_biz.adapters.ticket_http import TicketHttpEnvironment
from agentcheck_biz.checks import load_json, observe_database
from agentcheck_biz.commit_loss.integration import CommitLossEnvironment
from agentcheck_biz.events import EventLog
from agentcheck_biz.fault_proxy.integration import ProxyEnvironment
from agentcheck_biz.provenance import REPO_ROOT, implementation_digest
from agentcheck_biz.reports import save_json
from .policy import Budget, Contract, Outcome, RecoveryPolicy, classify_http
from .verify import audit_trace


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TicketRecoveryAdapter:
    def __init__(self, context, service, origin, contract, events, *, reject_identity=False, proxy=None, trust_proxy_receipts=True):
        self.context, self.service, self.origin, self.contract, self.events = context, service, origin, contract, events
        self.reject_identity, self.proxy = reject_identity, proxy
        self.trust_proxy_receipts = trust_proxy_receipts
        self.calls = 0

    def __call__(self, action, scope, request, attempt_id, remaining):
        if action not in {"create", "query"}:
            raise ValueError("This deterministic adapter has no model provider")
        self.calls += 1
        call_id = f"call-{self.calls}"
        # Both identity and original deadline cross the actual HTTP boundary.
        context = replace(self.context, attempt_id=attempt_id,
                          deadline=min(self.context.deadline, time.time() + remaining))
        transport = HttpTransport(self.origin, context, self.events, HttpTimeouts())
        token = "Bearer invalid-test-identity" if self.reject_identity else self.service.tokens[scope["tenant_id"]]
        self.events.record("recovery_http_call", call_id=call_id, action=action, attempt_id=attempt_id,
                           original_deadline=self.context.deadline, **scope)
        evidence = "http-client.jsonl#" + call_id
        try:
            status, value = transport.request("POST" if action == "create" else "GET", "/tickets",
                token=token, request_id=call_id, operation_id=scope["operation_id"], body=request if action == "create" else None)
        except (TimeoutError, httpx.TransportError) as error:
            if self.no_forward_receipt(call_id, attempt_id):
                return Outcome("not_forwarded", evidence="proxy-events.jsonl#" + call_id)
            return Outcome("outcome_unknown", evidence=evidence + ":" + type(error).__name__)
        # This is a local test-driver contract, not a rule about arbitrary HTTP 503.
        # The driver accepts no-forward only from its owned proxy's correlated event.
        return classify_http(status, value, self.contract, evidence)

    def no_forward_receipt(self, call_id, attempt_id):
        if not self.proxy or not self.trust_proxy_receipts:
            return False
        receipts = read_rows(self.context.evidence_dir / "proxy-events.jsonl")
        selected = [r for r in receipts if r.get("call_id") == call_id and r.get("attempt_id") == attempt_id]
        return (any(r["event"] == "downstream_aborted" and r.get("phase") == "before_forward" for r in selected)
                and not any(r["event"] == "upstream_forwarded" for r in selected))


def run_case(output, *, fault="confirmed_loss", verify_first=True, app_version="unsafe", max_tool_calls=6):
    if fault not in {"confirmed_loss", "reject", "unqualified_reject", "permanent", "none"}:
        raise ValueError("Unknown recovery experiment")
    run_id = "recovery-" + uuid4().hex
    directory = Path(output).resolve() / run_id
    directory.mkdir(parents=True)
    case = load_json(REPO_ROOT / "cases/tickets/full/T01.json")
    context = RunContext(run_id, case["context"]["operation_id"], "attempt-" + uuid4().hex, time.time() + 45, directory)
    events = EventLog(directory / "events.jsonl", run_id)
    service = TicketHttpEnvironment(case, app_version, HttpTimeouts())
    environment, proxy = service, None
    if fault in {"confirmed_loss", "reject", "unqualified_reject"}:
        rule = (load_json(REPO_ROOT / "cases/commit_loss/ticket.json") if fault == "confirmed_loss"
                else load_json(REPO_ROOT / "cases/proxy/P01_reject.json")["rule"])
        def target(ctx):
            return {"origin": service.transport.origin, "identity": service.identity,
                    "client_logs": ["http-client.jsonl"], "observer_source": "sqlite-readonly:business.sqlite",
                    "observer_http_logs": [], "routes": {"create_ticket": {"method": "POST", "path_pattern": "/tickets"},
                    "query_tickets": {"method": "GET", "path_pattern": "/tickets"}}}
        proxy = ProxyEnvironment(service, {"rule": rule}, target)
        environment = CommitLossEnvironment(proxy, case, "sqlite_commit") if fault == "confirmed_loss" else proxy
    budget = Budget(context.deadline, max_tool_calls, 0)
    contract = Contract("ticket-http/1:" + app_version, app_version == "fixed", (), (403, 409, 422))
    trace_log = EventLog(directory / "recovery-events.jsonl", run_id)
    policy = RecoveryPolicy(case["context"], case["request"], contract, budget,
        verify_first=verify_first, record=lambda row: trace_log.record("recovery", row=row), attempt_prefix=context.attempt_id)
    manifest = {"schema_version": 1, "run_id": run_id, "context": context.to_dict(), "case": case,
                "app_version": app_version, "fault": fault, "verify_first": verify_first,
                "implementation_sha256": implementation_digest(), "model_requests": 0}
    save_json(directory / "recovery-run.json", manifest)
    try:
        environment.prepare(context, events)
        save_json(directory / "initial.json", observe_database(directory / "business.sqlite"))
        adapter = TicketRecoveryAdapter(context, service, proxy.proxy.origin if proxy else service.transport.origin,
            contract, EventLog(directory / "http-client.jsonl", run_id), reject_identity=fault == "permanent", proxy=proxy,
            trust_proxy_receipts=fault != "unqualified_reject")
        result = policy.run(adapter)
        save_json(directory / "client.json", result)
        save_json(directory / "final.json", observe_database(directory / "business.sqlite"))
    finally:
        environment.cleanup(context, events)
    check = recheck(directory)
    save_json(directory / "recovery-checks.json", check)
    return {"run_dir": str(directory), "fault": fault, "verify_first": verify_first,
            "client_status": result["status"], "tool_calls": budget.tool_calls, **check}


def recheck(directory):
    """Read evidence only. Policy compliance and business truth remain distinct."""
    directory = Path(directory)
    try:
        run, final, initial, client = (load_json(directory / name) for name in
            ("recovery-run.json", "final.json", "initial.json", "client.json"))
        rows = [r["row"] for r in read_rows(directory / "recovery-events.jsonl")]
        check = audit_trace(rows)
        violations = list(check["violations"])
        scope, request = run["case"]["context"], run["case"]["request"]
        calls = [r for r in rows if r["event"] == "call"]
        outcomes = [r for r in rows if r["event"] == "outcome"]
        transport = read_rows(directory / "http-client.jsonl")
        bindings = [r for r in transport if r["event"] == "recovery_http_call"]
        sent = [r for r in transport if r["event"] == "http_request_started"]
        replies = {r["request_id"]: r for r in transport if r["event"] in {"http_response_received", "http_request_failed"}}
        if (len(calls) != len(sent) or len(replies) != len(sent) or len(outcomes) != len(calls) or len(bindings) != len(calls)
                or client != rows[-1]["result"] or any(rows[0].get(k) != v for k, v in scope.items())):
            violations.append("transport_or_terminal_count")
        proxy = read_rows(directory / "proxy-events.jsonl") if (directory / "proxy-events.jsonl").exists() else []
        for call, outcome, wire, binding in zip(calls, outcomes, sent, bindings):
            reply = replies[wire["request_id"]]
            if (call["attempt_id"] != wire["attempt_id"] or wire["method"] != ("POST" if call["action"] == "create" else "GET")
                    or any(binding[k] != v for k, v in scope.items())
                    or binding["attempt_id"] != call["attempt_id"] or binding["call_id"] != wire["request_id"]
                    or binding["original_deadline"] != run["context"]["deadline"]):
                violations.append("wire_identity")
            kind = outcome["kind"]
            if kind == "not_forwarded":
                selected = [r for r in proxy if r.get("call_id") == wire["request_id"] and r.get("attempt_id") == call["attempt_id"]]
                if not any(r["event"] == "downstream_aborted" and r.get("phase") == "before_forward" for r in selected) or any(r["event"] == "upstream_forwarded" for r in selected):
                    violations.append("unsupported_no_forward")
            elif kind == "success" and reply.get("status_code") != 200:
                violations.append("unsupported_http_success")
            elif kind == "permanent_rejection" and reply.get("status_code") not in {403, 409, 422}:
                violations.append("unsupported_permanent_rejection")
            elif kind == "outcome_unknown" and reply.get("status_code") == 200:
                violations.append("unsupported_http_unknown")
        effects = [r for r in final["tickets"] if all(r[k] == v for k, v in scope.items())]
        unrelated = lambda state: [r for r in state["tickets"] if not all(r[k] == v for k, v in scope.items())]
        if unrelated(initial) != unrelated(final) or observe_database(directory / "business.sqlite") != final:
            violations.append("independent_state_changed")
        if not load_json(directory / "http-cleanup.json")["exited"]:
            violations.append("service_cleanup")
        service_rows = read_rows(directory / "http-service-events.jsonl")
        received = [r for r in service_rows if r["event"] == "request_received" and r["path"] == "/tickets"]
        effect_receipts = [r for r in service_rows if r["event"] in {"ticket_created", "ticket_replayed"}]
        forwarded = [r for r in proxy if r["event"] == "upstream_forwarded"] if proxy else sent
        if {(r["request_id"], r["attempt_id"]) for r in received} != {(r["request_id"], r["attempt_id"]) for r in forwarded}:
            violations.append("service_request_correlation")
        if any(r["ticket"] not in final["tickets"] for r in effect_receipts):
            violations.append("service_receipt_not_in_database")
        if proxy:
            if not load_json(directory / "proxy-cleanup.json")["exited"]:
                violations.append("proxy_cleanup")
            if run["fault"] == "confirmed_loss":
                if not (any(r["event"] == "confirmation_received" for r in proxy)
                        and any(r["event"] == "downstream_aborted" for r in proxy)
                        and load_json(directory / "commit-loss-decision.json")["confirmed"]):
                    violations.append("missing_confirmed_drop")
                decision = load_json(directory / "commit-loss-decision.json")
                committed = load_json(directory / "commit-loss-ticket-ready.json")
                first = sent[0]
                if (any(decision[k] != committed[k] for k in ("run_id", "nonce", "call_id", "attempt_id", "operation_id"))
                        or decision["attempt_id"] != first["attempt_id"] or decision["record"] != committed["ticket"]
                        or decision["record"] not in decision["snapshot"]["tickets"]
                        or decision["record"] not in final["tickets"]):
                    violations.append("confirmation_identity_or_state")
        if len(effects) > 1 or any(any(r[k] != v for k, v in request.items()) for r in effects):
            business = "FAIL"
        elif client["status"] == "completed":
            business = "PASS" if len(effects) == 1 and effects[0] == client["value"] else "FAIL"
        elif client["status"] == "blocked" and not effects:
            business = "PASS"
        else:
            business = "INCONCLUSIVE"
        return {"policy_status": "FAIL" if violations else "PASS", "violations": sorted(set(violations)),
                "business_status": business, "resource_count": len(effects)}
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        return {"policy_status": "ERROR", "business_status": "INCONCLUSIVE", "resource_count": None,
                "violations": ["missing_or_invalid_evidence:" + type(error).__name__]}

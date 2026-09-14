"""Opt-in D22 comparison; no generic D23 retry/error policy is implied."""

from uuid import uuid4

from agentcheck_biz.adapters.contracts import PluginConfigurationError
from agentcheck_biz.adapters.mcp_transport import McpOutcomeUnknown
from .barrier import Controller


def validate_commit_loss(options, case, tool):
    if "commit_loss" not in options:
        return False
    if options["commit_loss"] is not True or set(options.get("proxy", {})) != {"rule"}:
        raise PluginConfigurationError("D22 requires commit_loss=true and an explicit proxy rule/control")
    rule = options["proxy"]["rule"]
    if rule is not None and (not isinstance(rule, dict) or rule.get("schema_version") != 2
                             or rule.get("tool") != tool or rule.get("run_id") != "$run_id"):
        raise PluginConfigurationError("D22 requires the object's confirmed-drop v2 rule")
    if case.get("scenario", "retry") != ("retry" if tool == "create_ticket" else "create"):
        raise PluginConfigurationError("D22 supports the single create comparison only")
    if tool == "create_ticket" and (case["limits"].get("max_client_attempts") != 2 or case["limits"]["max_tool_calls"] < 2):
        raise PluginConfigurationError("D22 comparison requires two bounded client attempts")
    return True


class CommitLossEnvironment:
    def __init__(self, proxy_environment, case, kind):
        self.base, self.case, self.kind = proxy_environment, case, kind
        self.controller = None

    def prepare(self, context, events):
        if self.base.options["rule"] is not None:
            config = {"kind": self.kind, "nonce": uuid4().hex}
            self.base.confirmation = config
            if self.kind == "sqlite_commit":
                self.base.base.test_commit_barrier = config
        self.base.prepare(context, events)
        if self.base.options["rule"] is not None:
            self.controller = Controller(context, self.base.confirmation, self.base.base, self.case)
            self.controller.thread.start()

    def cleanup(self, context, events):
        try:
            if self.controller:
                self.controller.close()
        finally:
            self.base.cleanup(context, events)


class ComparisonBehavior:
    def __init__(self, base, case, kind):
        self.base, self.case, self.kind = base, case, kind

    def __getattr__(self, name):
        return getattr(self.base, name)

    async def run(self, call, events):
        tool = "create_ticket" if self.kind == "sqlite_commit" else "create_issue"
        try:
            first = await call(0, tool, self.case["request"])
        except McpOutcomeUnknown as error:
            events.record("client_outcome_unknown", **error.detail)
            if self.kind != "sqlite_commit":
                raise  # Gitea API visibility only; no service-side idempotency claim.
            events.record("commit_loss_retry_scheduled", prior_call_id=error.detail["call_id"],
                          operation_id=error.detail["operation_id"], maximum_attempts=2)
            first = await call(0, tool, self.case["request"])
        if self.kind == "sqlite_commit":
            if first["status_code"] != 200:
                raise RuntimeError("D22 retry did not deliver a successful ticket")
            events.record("client_completed", ticket_id=first["value"]["ticket_id"])
            return {"status": "completed", "ticket_id": first["value"]["ticket_id"]}
        return {"status": "completed", "issue_number": first["issue"]["number"], "issue_id": first["issue"]["id"]}


def attach_commit_loss(plugin, kind):
    plugin.environment = CommitLossEnvironment(plugin.environment, plugin.case, kind)
    plugin.execution.behavior = ComparisonBehavior(plugin.execution.behavior, plugin.case, kind)
    plugin.run_metadata.update(commit_loss_version=1, commit_loss_kind=kind,
                               mode="D22 提交/可见性确认后真实断连；工单最多两次同操作创建；无模型")
    return plugin

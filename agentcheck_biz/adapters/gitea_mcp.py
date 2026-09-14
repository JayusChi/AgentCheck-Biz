"""Issue-specific binding and scenarios, with the existing independent oracle."""

import os
import re

from .gitea import gitea_plugin
from .mcp_execution import McpExecution


class GiteaMcpBehavior:
    def __init__(self, environment, case):
        self.environment, self.case = environment, case

    def bindings(self, context):
        return [{"backend": "gitea", "origin": self.environment.settings.origin,
                 "execution_token": self.environment.settings.execution_token,
                 "scope": {"operation_id": context.operation_id}, "repository": self.environment.repository}]

    def delivered(self, events, call_id, tool, result):
        events.record("tool_result_delivered", call_id=call_id, **result)

    async def run(self, call, events):
        result = await call(0, "create_issue", self.case["request"])
        if self.case["scenario"] == "duplicate":
            result = await call(0, "create_issue", self.case["request"])
        elif self.case["scenario"] == "close":
            result = await call(0, "close_issue", {"number": result["issue"]["number"]})
        return {"status": "completed", "issue_number": result["issue"]["number"], "issue_id": result["issue"]["id"]}

    def metadata(self, calls):
        return {"api_request_counts": {"execution": calls,
            **{role: getattr(self.environment, attribute).requests if getattr(self.environment, attribute) else 0
               for role, attribute in (("management", "management"), ("observer", "observer_client"))}}}


def gitea_mcp_plugin(case, options):
    from agentcheck_biz.commit_loss.integration import validate_commit_loss, attach_commit_loss
    enabled = validate_commit_loss(options, case, "create_issue")
    plugin = gitea_plugin(case, {key: value for key, value in options.items() if key not in {"proxy", "commit_loss"}})
    if enabled:
        plugin.run_metadata["commit_loss_enabled"] = True
    plugin.name, plugin.run_prefix = "gitea-mcp", "gitea-mcp"
    plugin.environment.execution_client_factory = None
    plugin.run_metadata.update(transport="mcp-stdio", backend_transport="http", runner_pid=os.getpid(),
        mode="确定性客户端；官方 MCP SDK / stdio 独立进程；转发 Gitea API；独立只读 API 观察；无模型、无故障注入")
    plugin.execution = McpExecution(GiteaMcpBehavior(plugin.environment, plugin.case), plugin.case["limits"]["max_tool_calls"])
    if "proxy" in options:
        from agentcheck_biz.fault_proxy.integration import attach_proxy
        environment = plugin.environment

        def target(context):
            path = re.escape(environment.repo_path + "/issues")
            return {"origin": environment.settings.origin, "identity": {**environment.settings.public(), "repository": environment.repository},
                    "client_logs": ["gitea-execution.jsonl"], "observer_source": "gitea-api-readonly", "observer_http_logs": ["gitea-observer.jsonl"],
                    "routes": {"create_issue": {"method": "POST", "path_pattern": path},
                               "close_issue": {"method": "PATCH", "path_pattern": path + r"/[1-9][0-9]*"}}}

        plugin = attach_proxy(plugin, options["proxy"], target)
        return attach_commit_loss(plugin, "api_visibility") if enabled else plugin
    return plugin

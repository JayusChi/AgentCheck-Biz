"""Independent Gitea Issue object using the four D17 lifecycle contracts."""

import platform

from agentcheck_biz.gitea_cases import issue_body, validate_gitea_case
from agentcheck_biz.observers.gitea import GiteaObserver, normalize_issue, normalize_repository
from agentcheck_biz.reports import save_json
from agentcheck_biz.verifiers.gitea import GiteaBusinessVerifier
from .contracts import BusinessPlugin, PluginConfigurationError
from .gitea_client import GiteaClient, GiteaSettings


class GiteaEnvironment:
    def __init__(self, settings, case):
        self.settings, self.case = settings, case
        self.execution_client_factory = GiteaClient
        self.management = self.execution_client = self.observer_client = None
        self.repository = None

    def prepare(self, context, events):
        self.management = GiteaClient(self.settings, "management", context)
        if self.execution_client_factory is not None:
            self.execution_client = self.execution_client_factory(self.settings, "execution", context)
        self.observer_client = GiteaClient(self.settings, "observer", context)
        version = self.management.request("GET", "/api/v1/version")
        if version["body"] != {"version": self.settings.version}:
            raise RuntimeError("Gitea actual version differs from configured version")
        identity = self.management.request("GET", "/api/v1/user")["body"]
        if identity["login"] != self.settings.owner or identity["is_admin"]:
            raise RuntimeError("A dedicated non-admin repository owner is required")
        self.repo_path = f"/api/v1/repos/{self.settings.owner}/{context.run_id}"
        self.repo_marker = f"AgentCheck test {self.settings.instance_id} {context.run_id}"
        created = self.management.request("POST", "/api/v1/user/repos", expected=201, body={
            "name": context.run_id, "private": True, "auto_init": False, "description": self.repo_marker})
        self.repository = normalize_repository(created["body"])
        if (self.repository["full_name"] != f"{self.settings.owner}/{context.run_id}"
                or self.repository["private"] is not True or self.repository["description"] != self.repo_marker):
            raise RuntimeError("Gitea did not allocate the requested isolated repository")
        save_json(context.evidence_dir / "gitea-target.json", {**self.settings.public(), "repository": self.repository,
                  "run_id": context.run_id, "evidence_level": "independent-readonly-api", "version_evidence": version["evidence"]})
        for row in self.case["initial_issues"]:
            self.management.request("POST", self.repo_path + "/issues", expected=201, body={
                "title": row["title"], "body": issue_body(context, row["operation_id"], row["body"]), "closed": row["state"] == "closed"})
        events.record("gitea_environment_prepared", repository=self.repository, version=self.settings.version)

    def cleanup(self, context, events):
        for client in (self.management, self.execution_client, self.observer_client):
            if client is not None:
                client.close()
        self.settings = None
        save_json(context.evidence_dir / "gitea-cleanup.json", {"run_id": context.run_id, "connections_closed": True,
            "repository_retained": self.repository, "service_ownership": "integration harness"})
        events.record("gitea_clients_closed", repository_retained=True)


class GiteaExecution:
    def __init__(self, environment, case):
        self.environment, self.case, self.call_count = environment, case, 0

    def execute(self, context, events):
        def call(method, path, body, expected):
            context.require_time()
            if self.call_count >= self.case["limits"]["max_tool_calls"]:
                raise RuntimeError("Gitea tool budget exhausted")
            self.call_count += 1
            call_id = f"call-{self.call_count}"
            events.record("tool_called", tool="create_issue" if method == "POST" else "close_issue", call_id=call_id,
                          operation_id=context.operation_id, attempt_id=context.attempt_id + "/" + call_id)
            response = self.environment.execution_client.request(method, path, body=body, expected=expected)
            issue = normalize_issue(response["body"], self.environment.repository)
            events.record("tool_result_delivered", call_id=call_id, issue=issue, evidence=response["evidence"])
            return issue

        path = self.environment.repo_path + "/issues"
        body = {"title": self.case["request"]["title"],
                "body": issue_body(context, context.operation_id, self.case["request"]["body"])}
        issue = call("POST", path, body, 201)
        if self.case["scenario"] == "duplicate":
            issue = call("POST", path, body, 201)
        elif self.case["scenario"] == "close":
            issue = call("PATCH", path + f"/{issue['number']}", {"state": "closed"}, 201)
        return {"status": "completed", "issue_number": issue["number"], "issue_id": issue["id"]}

    def metadata(self):
        return {"tool_calls": self.call_count,
                "api_request_counts": {role: getattr(self.environment, attr).requests if getattr(self.environment, attr) else 0
                    for role, attr in (("management", "management"), ("execution", "execution_client"), ("observer", "observer_client"))}}


def gitea_plugin(case, options):
    if options:
        raise PluginConfigurationError("Gitea credentials and dedicated target must come from environment variables")
    config = validate_gitea_case(case)
    settings = GiteaSettings.from_environment()
    environment = GiteaEnvironment(settings, config)
    return BusinessPlugin("gitea", 1, config, config["operation_id"], 90, "gitea-issue",
        {"mode": "确定性客户端；官方 Gitea HTTP API；独立只读 API 观察；无模型、无故障注入",
         "schema_version": 1, "case_id": config["case_id"], "agent": "scripted", "model_called": False,
         "app_version": settings.version, "python_version": platform.python_version(),
         "object_type": "gitea-issue", "evidence_level": "independent-readonly-api", "http_auto_retries": 0},
        environment, GiteaExecution(environment, config), GiteaObserver(environment, config), GiteaBusinessVerifier())

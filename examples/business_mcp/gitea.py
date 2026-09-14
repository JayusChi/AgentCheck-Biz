"""Gitea business tools; execution credentials cannot perform oracle reads."""

from types import SimpleNamespace

from mcp.types import Tool

from agentcheck_biz.adapters.gitea_client import GiteaClient
from agentcheck_biz.gitea_cases import issue_body
from agentcheck_biz.observers.gitea import normalize_issue


class GiteaBackend:
    def __init__(self, config, context):
        self.repository = config["repository"]
        self.client = GiteaClient(SimpleNamespace(origin=config["origin"], execution_token=config["execution_token"]),
                                  "execution", context)
        self.path = "/api/v1/repos/" + self.repository["full_name"] + "/issues"

    def tools(self):
        return [Tool(name="create_issue", description="Create an issue in the bound test repository.", inputSchema={
            "type": "object", "properties": {"title": {"type": "string", "minLength": 1}, "body": {"type": "string"}},
            "required": ["title", "body"], "additionalProperties": False}),
            Tool(name="close_issue", description="Close an issue in the bound test repository.", inputSchema={
                "type": "object", "properties": {"number": {"type": "integer", "minimum": 1}},
                "required": ["number"], "additionalProperties": False})]

    def call(self, tool, arguments, context, call_id):
        self.client.context = context
        if tool == "create_issue":
            if "<!-- agentcheck:" in arguments["body"]:
                raise ValueError("Reserved operation marker in issue body")
            response = self.client.request("POST", self.path, expected=201, tool_name=tool, call_id=call_id, body={
                "title": arguments["title"], "body": issue_body(context, context.operation_id, arguments["body"])})
        else:
            response = self.client.request("PATCH", self.path + f"/{arguments['number']}", expected=201,
                                           tool_name=tool, call_id=call_id, body={"state": "closed"})
        return {"issue": normalize_issue(response["body"], self.repository), "evidence": response["evidence"]}

    def close(self):
        self.client.close()

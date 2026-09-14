"""Scoped, bounded HTTP API access with response evidence and no token logging."""

import asyncio
from dataclasses import dataclass, field
import os
import re
import time
from urllib.parse import urlsplit

import httpx

from agentcheck_biz.events import EventLog
from agentcheck_biz.reports import save_json
from .contracts import PluginConfigurationError


@dataclass(frozen=True)
class GiteaSettings:
    origin: str
    owner: str
    instance_id: str
    version: str
    management_token: str = field(repr=False)
    execution_token: str = field(repr=False)
    observer_token: str = field(repr=False)

    @classmethod
    def from_environment(cls):
        try:
            if os.environ.get("GITEA_TEST_ONLY") != "1":
                raise ValueError("Dedicated test instance must be explicitly enabled")
            result = cls(*(os.environ["GITEA_" + name] for name in (
                "ORIGIN", "OWNER", "INSTANCE_ID", "EXPECTED_VERSION", "MANAGEMENT_TOKEN", "EXECUTION_TOKEN", "OBSERVER_TOKEN")))
            address = urlsplit(result.origin)
            if (address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port
                    or address.username or address.password or address.path or address.query or address.fragment
                    or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,60}", result.owner)
                    or not re.fullmatch(r"gitea-[a-f0-9]{32}", result.instance_id)
                    or not re.fullmatch(r"\d+\.\d+\.\d+", result.version)):
                raise ValueError("Invalid dedicated Gitea target configuration")
            tokens = (result.management_token, result.execution_token, result.observer_token)
            if len(set(tokens)) != 3 or any(not re.fullmatch(r"[a-f0-9]{40}", token) for token in tokens):
                raise ValueError("Three distinct scoped Gitea identities are required")
            return result
        except (KeyError, ValueError) as error:
            raise PluginConfigurationError("Gitea requires valid loopback test configuration and three environment tokens") from error

    def public(self):
        return {"origin": self.origin, "owner": self.owner, "instance_id": self.instance_id, "version": self.version}


class GiteaClient:
    def __init__(self, settings, role, context):
        self.origin, self.role, self.context = settings.origin, role, context
        self._token = getattr(settings, role + "_token")
        self.requests = 0
        self.log = EventLog(context.evidence_dir / f"gitea-{role}.jsonl", context.run_id)
        (context.evidence_dir / "gitea-api").mkdir(exist_ok=True)

    def request(self, method, path, *, params=None, body=None, expected=200, tool_name=None, call_id=None):
        if not path.startswith("/api/v1/") or "?" in path or ".." in path or "#" in path:
            raise ValueError("Gitea API path must be an explicit local route")
        if self.role == "observer" and method != "GET":
            raise ValueError("Observer is read-only")
        self.context.require_time()
        budget = min(5, self.context.deadline - time.time())
        if budget <= 0:
            raise TimeoutError("Gitea request deadline exhausted")
        self.requests += 1
        request_id = f"{self.role}-{self.requests}"
        evidence = f"gitea-api/{request_id}.json"
        self.log.record("api_request", request_id=request_id, method=method, path=path,
                        params=params, attempt_id=self.context.attempt_id, evidence=evidence, origin=self.origin,
                        pid=os.getpid(), call_id=call_id, tool=tool_name)
        headers = {"Authorization": "token " + self._token}
        if tool_name is not None:
            headers.update({"X-AgentCheck-Tool": tool_name, "X-Call-Id": call_id,
                            "X-Run-Id": self.context.run_id, "X-Request-Id": request_id,
                            "X-Attempt-Id": self.context.attempt_id, "X-Operation-Id": self.context.operation_id})

        async def send():
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0), trust_env=False,
                    follow_redirects=False, timeout=httpx.Timeout(min(3, budget), connect=min(1, budget))) as client:
                response = await client.request(method, self.origin + path,
                    headers=headers, params=params, json=body)
                return {"request_id": request_id, "role": self.role, "method": method, "path": path,
                    "params": params, "status_code": response.status_code,
                    "headers": {key: response.headers.get(key) for key in ("x-total-count", "link")},
                    "body": response.json() if response.content else None}

        async def bounded():
            return await asyncio.wait_for(send(), timeout=budget)

        try:
            result = asyncio.run(bounded())
        except Exception as error:
            self.log.record("api_error", request_id=request_id, error_type=type(error).__name__, pid=os.getpid(),
                            call_id=call_id, attempt_id=self.context.attempt_id, origin=self.origin)
            if isinstance(error, (httpx.TimeoutException, asyncio.TimeoutError)):
                raise TimeoutError("Gitea API deadline exceeded; outcome requires observation") from error
            raise
        save_json(self.context.evidence_dir / evidence, result)
        self.log.record("api_response", request_id=request_id, status_code=result["status_code"], evidence=evidence)
        if result["status_code"] != expected:
            raise RuntimeError(f"Gitea {self.role} API returned {result['status_code']}; expected {expected}")
        return {**result, "evidence": evidence}

    def close(self):
        # Each request has already closed its async HTTP client.
        self._token = ""

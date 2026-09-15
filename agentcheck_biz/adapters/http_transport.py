"""A single real HTTP attempt with phase timeouts and an overall deadline."""

import asyncio
from dataclasses import dataclass
import math
import os
import time
from urllib.parse import urlsplit

import httpx

from .contracts import PluginConfigurationError


@dataclass(frozen=True)
class HttpTimeouts:
    connect: float = 1.0
    read: float = 2.0
    total: float = 5.0

    def __post_init__(self):
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 < v <= 30
               for v in (self.connect, self.read, self.total)):
            raise PluginConfigurationError("HTTP timeouts must be finite, positive and at most 30 seconds")


class HttpTransport:
    def __init__(self, origin, context, events, timeouts):
        address = urlsplit(origin)
        if (address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port
                or address.username or address.password or address.path or address.query or address.fragment):
            raise ValueError("Only the allocated loopback test origin is supported")
        self.origin, self.context, self.events, self.timeouts = origin, context, events, timeouts
        self.attempts = 0
        self.business_attempts = 0

    async def _send(self, method, path, headers, body, budget):
        timeout = httpx.Timeout(connect=min(self.timeouts.connect, budget),
                                read=min(self.timeouts.read, budget), write=min(self.timeouts.read, budget),
                                pool=min(self.timeouts.connect, budget))
        # No redirect follow-up, proxy routing, connection retry or hidden SDK loop.
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0),
                                     trust_env=False, follow_redirects=False, timeout=timeout) as client:
            response = await client.request(method, self.origin + path, headers=headers, json=body)
            return response.status_code, response.json()

    def request(self, method, path, *, token, request_id, operation_id=None, body=None):
        if (method, path) not in {("GET", "/health"), ("GET", "/version"),
                                 ("GET", "/tickets"), ("POST", "/tickets")}:
            raise ValueError("Unsupported ticket HTTP route")
        self.context.require_time()
        budget = min(self.timeouts.total, self.context.deadline - time.time())
        if budget <= 0:
            raise TimeoutError("HTTP deadline exhausted")
        self.attempts += 1
        self.business_attempts += int(path == "/tickets")
        details = {"request_id": request_id, "attempt_id": self.context.attempt_id,
                   "method": method, "path": path, "origin": self.origin, "pid": os.getpid()}
        self.events.record("http_request_started", **details, total_budget_seconds=budget)
        headers = {"Authorization": token, "X-Run-Id": self.context.run_id,
                   "X-Request-Id": request_id, "X-Attempt-Id": self.context.attempt_id}
        if operation_id is not None:
            headers["X-Operation-Id"] = operation_id
        if path == "/tickets":
            headers.update({"X-AgentCheck-Tool": "create_ticket" if method == "POST" else "query_tickets", "X-Call-Id": request_id})

        async def bounded():
            return await asyncio.wait_for(self._send(method, path, headers, body, budget), timeout=budget)

        try:
            status, data = asyncio.run(bounded())
        except (asyncio.TimeoutError, httpx.TimeoutException) as error:
            self.events.record("http_request_failed", **details, error_type=type(error).__name__)
            raise TimeoutError("HTTP request timed out; business outcome requires observation") from error
        except Exception as error:
            self.events.record("http_request_failed", **details, error_type=type(error).__name__)
            raise
        self.events.record("http_response_received", **details, status_code=status)
        return status, data

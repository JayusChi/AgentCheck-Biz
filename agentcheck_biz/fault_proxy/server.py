"""Bounded HTTP/1.1 reverse proxy with actual downstream socket interruption."""

import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

import h11

from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.events import EventLog
from agentcheck_biz.provenance import implementation_digest
from agentcheck_biz.reports import save_json
from . import PROXY_VERSION
from .rules import loopback, matches, validate_rule

MAX_BODY = 4 * 1024 * 1024
HOP_HEADERS = {b"connection", b"proxy-connection", b"keep-alive", b"te", b"trailer", b"transfer-encoding", b"upgrade"}


async def wait_delay(delay_ms):
    # Event-loop timers may wake early at Windows' clock resolution. Re-check
    # the promised deadline instead of treating one sleep as elapsed-time proof.
    end = time.monotonic_ns() + delay_ms * 1_000_000
    while (remaining := end - time.monotonic_ns()) > 0:
        await asyncio.sleep(max(.001, remaining / 1_000_000_000))


def end_headers(headers):
    nominated = {part.strip().lower() for key, value in headers if key == b"connection" for part in value.split(b",")}
    return [(key, value) for key, value in headers if key not in HOP_HEADERS | nominated | {b"host", b"content-length"}]


def fingerprint(headers, body):
    # Preserve duplicate end-to-end headers; never log credential values.
    encoded = json.dumps(sorted((k.decode("ascii"), v.decode("latin1")) for k, v in end_headers(headers)), separators=(",", ":")).encode()
    return {"body_sha256": hashlib.sha256(body).hexdigest(), "body_bytes": len(body),
            "headers_sha256": hashlib.sha256(encoded).hexdigest()}


async def read_message(reader, protocol, expected):
    head, body = None, bytearray()
    while True:
        event = protocol.next_event()
        if event is h11.NEED_DATA:
            protocol.receive_data(await reader.read(65536))
        elif isinstance(event, expected):
            head = event
        elif isinstance(event, h11.Data):
            body.extend(event.data)
            if len(body) > MAX_BODY:
                raise ValueError("HTTP message exceeds test proxy limit")
        elif isinstance(event, h11.EndOfMessage):
            if head is None or event.headers:
                raise ValueError("Missing HTTP header or unsupported trailers")
            return head, bytes(body)
        elif isinstance(event, h11.InformationalResponse):
            raise ValueError("Informational/upgrade responses are not supported")
        else:
            raise ValueError("Connection closed or pipelined before complete HTTP message")


class ProxyServer:
    def __init__(self, config):
        self.context = RunContext(**{**config["context"], "evidence_dir": Path(config["context"]["evidence_dir"])})
        self.target, self.rule = config["target"], validate_rule(config["rule"])
        self.address = loopback(self.target["origin"])
        self.config = config
        self.log = EventLog(self.context.evidence_dir / "proxy-events.jsonl", self.context.run_id)
        self.connections, self.upstream_attempts, self.hits = 0, 0, 0
        self.numbers, self.seen, self.tasks = Counter(), set(), set()

    def record(self, event, **details):
        self.log.record(event, pid=os.getpid(), monotonic_ns=time.monotonic_ns(), **details)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.connections += 1
        connection_id = f"connection-{self.connections}"
        self.record("connection_accepted", connection_id=connection_id)
        try:
            await asyncio.wait_for(self.exchange(reader, writer, connection_id),
                                   timeout=max(.001, min(15, self.context.deadline - time.time())))
        except asyncio.CancelledError:
            self.record("connection_cancelled", connection_id=connection_id)
            raise
        except Exception as error:
            self.record("proxy_error", connection_id=connection_id, error_type=type(error).__name__)
            writer.transport.abort()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.record("connection_closed", connection_id=connection_id)
            self.tasks.discard(task)

    async def exchange(self, reader, writer, connection_id):
        protocol = h11.Connection(h11.SERVER, max_incomplete_event_size=32768)
        request, body = await read_message(reader, protocol, h11.Request)
        headers = dict(request.headers)
        text = lambda key: headers.get(key, b"").decode("ascii")
        tool, run_id = text(b"x-agentcheck-tool"), text(b"x-run-id")
        call_id, attempt_id = text(b"x-call-id"), text(b"x-attempt-id")
        request_id, operation_id = text(b"x-request-id"), text(b"x-operation-id")
        method, path = request.method.decode("ascii"), request.target.decode("ascii")
        route = self.target["routes"].get(tool)
        valid = (run_id == self.context.run_id and route and method == route["method"]
                 and re.fullmatch(route["path_pattern"], path) and len(attempt_id) <= 200 and attempt_id
                 and re.fullmatch(r"call-[1-9][0-9]*", call_id) and request_id and len(request_id) <= 100
                 and operation_id and len(operation_id) <= 200 and attempt_id not in self.seen)
        if not valid:
            self.record("scope_rejected", connection_id=connection_id)
            writer.transport.abort()
            return
        self.context.require_time()
        self.seen.add(attempt_id)
        self.numbers[tool] += 1
        details = {"connection_id": connection_id, "request_id": request_id, "call_id": call_id,
                   "attempt_id": attempt_id, "operation_id": operation_id, "tool": tool,
                   "method": method, "path": path, "request_number": self.numbers[tool]}
        self.record("proxy_received", **details, **fingerprint(request.headers, body))
        selected = matches(self.rule, run_id, tool, details["request_number"], self.hits)

        def trigger():
            self.hits += 1
            self.record("fault_triggered", **details, rule_id=self.rule["rule_id"], action=self.rule["action"],
                        phase=self.rule["phase"], trigger_number=self.hits, delay_ms=self.rule["delay_ms"])

        if selected and self.rule["action"] == "reject":
            trigger()
            self.record("downstream_aborted", **details, phase="before_forward", bytes_sent=0)
            writer.transport.abort()
            return
        self.upstream_attempts += 1
        upstream_id = f"upstream-{self.upstream_attempts}"
        self.record("upstream_connecting", **details, upstream_attempt_id=upstream_id, origin=self.target["origin"])
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self.address.port)
        try:
            upstream = h11.Connection(h11.CLIENT, max_incomplete_event_size=32768)
            forwarded_headers = end_headers(request.headers) + [(b"host", self.address.netloc.encode()),
                (b"content-length", str(len(body)).encode()), (b"connection", b"close")]
            for event in (h11.Request(method=request.method, target=request.target, headers=forwarded_headers),
                          h11.Data(data=body), h11.EndOfMessage()):
                upstream_writer.write(upstream.send(event))
            await upstream_writer.drain()
            self.record("upstream_forwarded", **details, upstream_attempt_id=upstream_id, **fingerprint(forwarded_headers, body))
            confirmed_rule = selected and self.rule["schema_version"] == 2
            if confirmed_rule and self.target["confirmation"]["kind"] == "sqlite_commit":
                if await self.confirmed_abort(details, writer, trigger):
                    return
            response, response_body = await read_message(upstream_reader, upstream, h11.Response)
            self.record("upstream_response", **details, upstream_attempt_id=upstream_id, status_code=response.status_code,
                        **fingerprint(response.headers, response_body))
            if confirmed_rule and self.target["confirmation"]["kind"] == "api_visibility":
                if await self.confirmed_abort(details, writer, trigger, response, response_body):
                    return
        finally:
            upstream_writer.close()
            try:
                await upstream_writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        if selected and self.rule["schema_version"] == 1 and matches(self.rule, run_id, tool, details["request_number"], self.hits):
            trigger()
            if self.rule["action"] == "drop":
                self.record("downstream_aborted", **details, phase="after_upstream_response", bytes_sent=0)
                writer.transport.abort()
                return
            self.record("delay_started", **details, delay_ms=self.rule["delay_ms"])
            await wait_delay(self.rule["delay_ms"])
            self.record("delay_finished", **details, delay_ms=self.rule["delay_ms"])
        response_headers = end_headers(response.headers) + [(b"content-length", str(len(response_body)).encode()), (b"connection", b"close")]
        for event in (h11.Response(status_code=response.status_code, headers=response_headers, reason=response.reason),
                      h11.Data(data=response_body), h11.EndOfMessage()):
            writer.write(protocol.send(event))
        await writer.drain()
        self.record("downstream_sent", **details, status_code=response.status_code, **fingerprint(response_headers, response_body))

    async def confirmed_abort(self, details, writer, trigger, response=None, body=None):
        from agentcheck_biz.commit_loss.barrier import KEYS, wait_message_async
        directory = self.context.evidence_dir
        identity = {**self.target["confirmation"], "run_id": self.context.run_id,
                    **{k: details[k] for k in KEYS if k not in {"nonce", "run_id", "kind"}}}
        if response is not None:
            save_json(directory / "commit-loss-upstream.json", {**identity,
                "status_code": response.status_code, "body": json.loads(body), "raw_body": body.decode("utf-8")})
        self.record("confirmation_waiting", **details, **self.target["confirmation"])
        save_json(directory / "commit-loss-proxy-ready.json", {**identity, "pid": os.getpid(),
                  "monotonic_ns": time.monotonic_ns()})
        decision = await wait_message_async(directory / "commit-loss-decision.json", identity,
                                             min(self.context.deadline, time.time() + 9))
        if decision.get("confirmed") is not True:
            self.record("confirmation_denied", **details)
            return False
        if decision.get("pid") != self.config["parent_pid"]:
            raise ValueError("Confirmation was not issued by the owning controller")
        self.record("confirmation_received", **details, **self.target["confirmation"],
                    decision_monotonic_ns=decision["monotonic_ns"])
        trigger()
        self.record("downstream_aborted", **details, phase="after_independent_confirmation", bytes_sent=0)
        writer.transport.abort()
        save_json(directory / "commit-loss-aborted.json", {**identity, "pid": os.getpid(),
                  "monotonic_ns": time.monotonic_ns(), "aborted": True})
        return True

    async def run(self):
        if self.config["parent_pid"] != os.getppid():
            raise RuntimeError("Proxy parent mismatch")
        server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        identity = {"version": PROXY_VERSION, "run_id": self.context.run_id, "pid": os.getpid(), "parent_pid": os.getppid(),
                    "nonce": self.config["nonce"], "host": "127.0.0.1", "port": server.sockets[0].getsockname()[1],
                    "upstream_origin": self.target["origin"], "implementation_sha256": implementation_digest()}
        self.record("proxy_started", identity=identity)
        save_json(self.context.evidence_dir / "proxy-ready.json", identity)
        try:
            async with server:
                while time.time() < self.context.deadline and not (self.context.evidence_dir / "proxy-stop").exists():
                    await asyncio.sleep(.02)
        finally:
            for task in list(self.tasks):
                task.cancel()
            if self.tasks:
                await asyncio.gather(*list(self.tasks), return_exceptions=True)
            summary = {"run_id": self.context.run_id, "nonce": self.config["nonce"], "connections": self.connections,
                       "upstream_attempts": self.upstream_attempts, "tool_requests": dict(self.numbers), "triggers": self.hits,
                       "coverage": "transparent" if self.rule is None else "covered" if self.hits else "not_covered"}
            self.record("proxy_stopped", summary=summary)
            save_json(self.context.evidence_dir / "proxy-summary.json", summary)


if __name__ == "__main__":
    asyncio.run(ProxyServer(json.loads(sys.stdin.buffer.readline())).run())

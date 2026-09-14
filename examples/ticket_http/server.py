"""One child process, one ephemeral listener, one database, test-only identities."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from agentcheck_biz.events import EventLog
from agentcheck_biz.provenance import implementation_digest
from agentcheck_biz.reports import save_json
from examples.ticket_agent.fixture import prepare_fixture
from examples.ticket_agent.fixed_service import FixedTicketService, IdempotencyConflict
from examples.ticket_agent.service import OperationContext, TicketService
from . import API_VERSION


class TicketFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=False)
    customer_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    description: str = Field(min_length=1)


def create_app(config, identity, events, *, service=None, crash_probe=None):
    service_type = FixedTicketService if config["app_version"] == "fixed" else TicketService
    service = service if service is not None else service_type(Path(config["evidence_dir"]) / "business.sqlite")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def audit(request, call_next):
        details = {"request_id": request.headers.get("x-request-id"),
                   "attempt_id": request.headers.get("x-attempt-id"),
                   "method": request.method, "path": request.url.path, "pid": os.getpid()}
        events.record("request_received", **details)
        response = await call_next(request)
        events.record("request_completed", **details, status_code=response.status_code)
        return response

    def authenticated(request: Request):
        if time.time() >= config["deadline"]:
            raise HTTPException(408, "Test run deadline exhausted")
        if (request.headers.get("x-run-id") != config["run_id"]
                or not request.headers.get("x-request-id")
                or not request.headers.get("x-attempt-id")):
            raise HTTPException(403, "Test run identity required")
        tenant = config["identities"].get(request.headers.get("authorization", ""))
        if tenant is None:
            raise HTTPException(403, "Test identity required")
        return tenant

    def operation(request: Request, tenant=Depends(authenticated)):
        # Operation ID comes from the harness, never the model's tool arguments.
        operation_id = request.headers.get("x-operation-id", "")
        if not operation_id.strip():
            raise HTTPException(422, "Operation ID required")
        if request.query_params:
            raise HTTPException(422, "Query parameters are not supported")
        return OperationContext(tenant, operation_id)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request, error):
        # Do not echo arbitrary inputs or authentication into persisted evidence.
        return JSONResponse(status_code=422, content={"detail": "Invalid ticket fields"})

    @app.get("/health", dependencies=[Depends(authenticated)])
    def health():
        return {"status": "ok", **identity}

    @app.get("/version", dependencies=[Depends(authenticated)])
    def version():
        return identity

    @app.post("/tickets")
    def create(fields: TicketFields, request: Request, context=Depends(operation)):
        if crash_probe is not None:
            crash_probe.bind(request)
        try:
            receipt = service.create_ticket_with_receipt(context, **fields.model_dump())
        except IdempotencyConflict:
            raise HTTPException(409, "Operation ID conflicts with existing content")
        except ValueError:
            raise HTTPException(422, "Ticket fields must be nonempty text")
        # Receipt is emitted only after the shared service transaction returns.
        # The platform still obtains persistence proof independently via SQLite RO.
        events.record("ticket_replayed" if receipt.deduplicated else "ticket_created",
                      request_id=request.headers["x-request-id"], pid=os.getpid(), ticket=receipt.ticket)
        if crash_probe is not None:
            crash_probe.after_commit(request, receipt)
        if config.get("test_commit_barrier"):
            from agentcheck_biz.commit_loss.barrier import ticket_barrier
            ticket_barrier(config, request, receipt, events)
        return receipt.ticket

    @app.get("/tickets")
    def query(context=Depends(operation)):
        return service.query_tickets(context)

    return app


async def serve(config):
    directory = Path(config["evidence_dir"]).resolve()
    if directory.name != config["run_id"] or config["app_version"] not in {"unsafe", "fixed"}:
        raise ValueError("Invalid isolated fixture configuration")
    events = EventLog(directory / "http-service-events.jsonl", config["run_id"])
    prepare_fixture(directory / "business.sqlite", config["run_id"], config["case"], config["app_version"])
    with socket.socket() as listener:
        # Keep the socket open from port allocation through Uvicorn ownership.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        identity = {"service": "ticket-http", "api_version": API_VERSION,
                    "app_version": config["app_version"], "run_id": config["run_id"],
                    "instance_id": config["instance_id"], "pid": os.getpid(),
                    "host": "127.0.0.1", "port": listener.getsockname()[1],
                    "implementation_sha256": implementation_digest()}
        events.record("service_initialized", identity=identity)
        server = uvicorn.Server(uvicorn.Config(create_app(config, identity, events), host="127.0.0.1",
                                log_level="warning", access_log=False, timeout_graceful_shutdown=2))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("HTTP service exited before readiness")
                if time.time() >= config["deadline"]:
                    raise TimeoutError("HTTP startup deadline exhausted")
                await asyncio.sleep(.02)
            # Atomic publication prevents the parent reading a half-written file.
            pending = directory / "http-ready.pending"
            save_json(pending, identity)
            pending.replace(directory / "http-ready.json")
            while not task.done():
                if time.time() >= config["deadline"] or os.getppid() != config["parent_pid"]:
                    server.should_exit = True
                await asyncio.sleep(.1)
            await task
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)


def main():
    # Ephemeral credentials travel through stdin, not argv, .env or saved config.
    config = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
    asyncio.run(serve(config))


if __name__ == "__main__":
    main()

"""Restartable test host, using the existing Ticket HTTP routes and fixed service."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import time

import uvicorn

from agentcheck_biz.events import EventLog
from agentcheck_biz.provenance import implementation_digest
from examples.ticket_agent.database import connect_database
from examples.ticket_agent.fixture import prepare_fixture
from examples.ticket_agent.fixed_service import FixedTicketService
from examples.ticket_http.server import create_app
from .barrier import CrashProbe, publish


def open_persistent_data(config, events):
    database = Path(config["data_dir"]) / "business.sqlite"
    if config["generation"] == 1:
        if database.exists():
            raise ValueError("First generation requires a new database")
        prepare_fixture(database, config["run_id"], config["case"], "fixed")
        event = "database_seeded"
    else:
        if not database.is_file():
            raise ValueError("Restart requires the original database; never reseed")
        # A writable connection lets SQLite recover a hot rollback journal after
        # a killed writer. No schema creation, seeding, copying or truncation.
        with connect_database(database) as connection:
            row = connection.execute("SELECT run_id FROM test_environment WHERE singleton = 1").fetchone()
            if row is None or row["run_id"] != config["run_id"]:
                raise ValueError("Foreign persistent database")
        event = "database_resumed"
    events.record(event, data_dir=config["data_dir"], database=str(database), generation=config["generation"])
    return database


async def serve(config):
    directory = Path(config["instance_dir"]).resolve()
    root, data = Path(config["root_dir"]).resolve(), Path(config["data_dir"]).resolve()
    if (root.name != config["run_id"] or data != root / "data" or directory.parent != root / "instances"
            or directory.name != config["instance_id"] or config["owner_pid"] != os.getppid()):
        raise ValueError("Invalid owned crash environment")
    events = EventLog(directory / "service-events.jsonl", config["run_id"])
    database = open_persistent_data(config, events)
    with socket.socket() as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        identity = {k: config[k] for k in ("run_id", "instance_id", "generation", "nonce", "data_dir", "owner_pid")}
        identity.update(service="ticket-http", api_version=1, app_version="fixed", host="127.0.0.1",
                        port=listener.getsockname()[1], pid=os.getpid(), implementation_sha256=implementation_digest())
        service = FixedTicketService(database)
        probe = CrashProbe(config, identity, events) if config["point"] in {"before_commit", "after_commit"} else None
        if probe:
            service._test_before_commit = probe.before_commit
        app = create_app(config, identity, events, service=service, crash_probe=probe)
        events.record("service_initialized", identity=identity)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, timeout_graceful_shutdown=1))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Service stopped during startup")
                if time.time() >= config["deadline"]:
                    raise TimeoutError("Service startup deadline")
                await asyncio.sleep(.01)
            publish(directory / "ready.json", identity)
            while not task.done():
                if time.time() >= config["deadline"] or os.getppid() != config["owner_pid"]:
                    server.should_exit = True
                await asyncio.sleep(.02)
            await task
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=3)


if __name__ == "__main__":
    asyncio.run(serve(json.loads(sys.stdin.buffer.readline().decode("utf-8"))))

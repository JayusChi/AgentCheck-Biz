"""Explicit HTTP side-effect node and PostgreSQL checkpoint boundary."""

from contextlib import contextmanager
import os
import time
from typing import TypedDict

import psycopg
from psycopg.rows import dict_row
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import START, END, StateGraph
from langsmith import tracing_context

from agentcheck_biz.adapters.contracts import RunContext
from agentcheck_biz.adapters.http_transport import HttpTransport, HttpTimeouts
from agentcheck_biz.events import EventLog
from .store import Store, BindingError, IDENTITY_KEYS


class TicketState(TypedDict):
    job_id: str
    experiment_id: str
    environment_id: str
    thread_id: str
    run_id: str
    operation_id: str
    evidence_dir: str
    request_sha256: str
    storage_version: int
    graph_version: str
    attempt_id: str
    phase: str
    ticket: dict | None
    create_calls: int
    query_calls: int
    model_requests: int


@contextmanager
def checkpointer(dsn, *, readonly=False):
    # setup is an explicit bootstrap operation, never part of a read or resume.
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row) as conn:
        if readonly:
            conn.execute("SET default_transaction_read_only=on")
        yield PostgresSaver(conn)


def config(manifest, attempt=None):
    return dict(configurable=dict(thread_id=manifest["thread_id"], checkpoint_ns="", attempt_id=attempt),
                callbacks=[], recursion_limit=12, metadata={k: manifest[k] for k in IDENTITY_KEYS})


def build(saver, manifest, attempt=None, transport=None, token=None, events=None, *, pause=False, gap=False):
    def event(name, **values):
        if events:
            events.record(name, job_id=manifest["job_id"], thread_id=manifest["thread_id"], attempt_id=attempt, **values)

    def prepare(state):
        return dict(phase="prepared", attempt_id=attempt)

    def submit(state):
        if state["create_calls"] != 0:
            raise BindingError("Side-effect node cannot automatically repeat a completed create")
        event("node_entered", node="submit")
        status, ticket = transport.request("POST", "/tickets", token=token, request_id=attempt + ":create",
            operation_id=manifest["operation_id"], body=manifest["case"]["request"])
        if status != 200 or not isinstance(ticket, dict) or not ticket.get("ticket_id"):
            raise RuntimeError("Create outcome requires verification")
        event("business_effect_received", ticket=ticket)
        if gap:
            # Deliberately fail before returning the node update to the saver.
            # A successful external commit is not a checkpoint transaction.
            raise RuntimeError("controlled_after_commit_before_checkpoint")
        return dict(phase="effect_observed", ticket=ticket, create_calls=1, attempt_id=attempt)

    def verify(state):
        event("node_entered", node="verify")
        status, tickets = transport.request("GET", "/tickets", token=token, request_id=attempt + ":query",
                                           operation_id=manifest["operation_id"])
        if status != 200 or tickets != [state["ticket"]]:
            raise RuntimeError("Independent result query disagrees with saved ticket")
        return dict(phase="verified", query_calls=state["query_calls"] + 1, attempt_id=attempt)

    def finish(state):
        return dict(phase="completed", attempt_id=attempt)

    graph = StateGraph(TicketState)
    for name, node in (("prepare", prepare), ("submit", submit), ("verify", verify), ("finish", finish)):
        graph.add_node(name, node)
    for left, right in ((START, "prepare"), ("prepare", "submit"), ("submit", "verify"), ("verify", "finish"), ("finish", END)):
        graph.add_edge(left, right)
    return graph.compile(checkpointer=saver, interrupt_after=["submit"] if pause else None)


def validate_snapshot(snapshot, manifest):
    if not snapshot.values or any(snapshot.values.get(k) != manifest[k] for k in IDENTITY_KEYS):
        raise BindingError("Missing checkpoint or foreign experiment/environment in checkpoint")
    if snapshot.values.get("model_requests") != 0:
        raise BindingError("Unexpected model activity in deterministic checkpoint")
    return snapshot


def snapshot_payload(snapshot):
    return dict(checkpoint_id=snapshot.config["configurable"]["checkpoint_id"], values=snapshot.values,
                next=list(snapshot.next), created_at=snapshot.created_at,
                pending_errors=[str(t.error) for t in snapshot.tasks if t.error])


def read_snapshot(dsn, manifest):
    job = Store(dsn).get(manifest)
    with checkpointer(dsn, readonly=True) as saver:
        graph = build(saver, manifest)
        snapshot = validate_snapshot(graph.get_state(config(manifest)), manifest)
        if not job["checkpoint_id"] or snapshot.config["configurable"]["checkpoint_id"] != job["checkpoint_id"]:
            raise BindingError("Coordinator checkpoint pointer and persisted thread disagree; reconciliation required")
        return dict(job_state=job["state"], job_revision=job["revision"], **snapshot_payload(snapshot))


def execute(dsn, manifest, output, *, action, origin, token, gap=False):
    store = Store(dsn)
    job = store.get(manifest)
    if action == "resume":
        saved = read_snapshot(dsn, manifest)
        if (job["state"] not in {"interrupted", "waiting_verification"} or saved["pending_errors"]
                or saved["values"]["phase"] not in {"effect_observed", "verified"}):
            raise BindingError("Resume is unsafe without a saved side-effect result; external verification required")
    elif action != "start" or job["state"] != "queued":
        raise BindingError("Start requires a queued job; resume must not recreate a job")
    attempt = store.begin(manifest)
    context = RunContext(manifest["run_id"], manifest["operation_id"], attempt, time.time() + 30,
                         __import__("pathlib").Path(manifest["evidence_dir"]))
    events = EventLog(output / "events.jsonl", context.run_id)
    transport = HttpTransport(origin, context, EventLog(output / "http-client.jsonl", context.run_id), HttpTimeouts())
    graph_error = None
    with checkpointer(dsn) as saver, tracing_context(enabled=False):
        graph = build(saver, manifest, attempt, transport, token, events, pause=action == "start", gap=gap)
        initial = {k: manifest[k] for k in IDENTITY_KEYS} | dict(attempt_id=attempt, phase="new", ticket=None,
                                                                create_calls=0, query_calls=0, model_requests=0)
        try:
            graph.invoke(initial if action == "start" else None, config(manifest, attempt), durability="sync")
        except Exception as error:
            graph_error = type(error).__name__ + ": " + str(error)
        snapshot = validate_snapshot(graph.get_state(config(manifest, attempt)), manifest)
        payload = snapshot_payload(snapshot)
    state = "waiting_verification" if graph_error else "interrupted" if payload["next"] else "finished"
    store.finish_attempt(manifest, attempt, state, payload["checkpoint_id"], graph_error or "checkpoint_saved")
    return dict(status="INCONCLUSIVE" if graph_error else "PASS", job_state=state, attempt_id=attempt,
                pid=os.getpid(), model_requests=0, error=graph_error, **payload)

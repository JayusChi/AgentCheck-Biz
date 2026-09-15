"""Offline oracle: compare saved PG channel blobs, Agent reports and SQLite truth."""

from pathlib import Path
import json

import ormsgpack

from agentcheck_biz.checks import load_json, observe_database
from .store import digest, IDENTITY_KEYS, TRANSITIONS, validate_manifest


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def checkpoint_values(database, thread, checkpoint_id):
    row = next(r for r in database["checkpoints"] if r["thread_id"] == thread
               and r["checkpoint_id"] == checkpoint_id and r["checkpoint_ns"] == "")
    versions = row["checkpoint"]["channel_versions"]
    values = {}
    for channel in (*IDENTITY_KEYS, "attempt_id", "phase", "ticket", "create_calls", "query_calls", "model_requests"):
        if channel in row["checkpoint"]["channel_values"]:
            values[channel] = row["checkpoint"]["channel_values"][channel]
            continue
        blob = next(r for r in database["checkpoint_blobs"] if r["thread_id"] == thread and r["checkpoint_ns"] == ""
                    and r["channel"] == channel and r["version"] == versions[channel])
        require(blob["type"] == "msgpack" and blob["blob"].startswith("\\x"), "Unsupported saved channel encoding")
        # These graph channels contain only ordinary dict/list/string/scalar data.
        # No pickle or object-import deserializer is needed for offline evidence.
        values[channel] = ormsgpack.unpackb(bytes.fromhex(blob["blob"][2:]))
    return values


def recheck(directory):
    directory = Path(directory).resolve()
    try:
        summary = load_json(directory / "summary.json")
        original = Path(summary["summary_path"]).parent
        def rebound(path):
            return directory / Path(path).relative_to(original)
        database = load_json(directory / "postgres-snapshot.json")
        require(digest(database) == summary["database_snapshot_sha256"], "PostgreSQL snapshot digest mismatch")
        require(len(database["ac_jobs"]) == 3 and len(summary["cleanup"]) == 2 and all(p["exited"] for p in summary["cleanup"]),
                "Missing jobs or PostgreSQL cleanup")
        states = []
        processes = 0
        for name, target_state, count in (("normal", "finished", 1), ("foreign", "queued", 0), ("gap", "waiting_verification", 1)):
            record = summary[name]
            run_dir = rebound(record["run_dir"])
            manifest = load_json(run_dir / "manifest.json")
            validate_manifest(manifest)
            require(manifest == record["manifest"], "Manifest differs from experiment summary")
            job = next(j for j in database["ac_jobs"] if j["job_id"] == manifest["job_id"])
            require(job["manifest"] == manifest and job["state"] == target_state, "Persisted job identity/state mismatch")
            timeline = sorted((r for r in database["ac_job_events"] if r["job_id"] == job["job_id"]), key=lambda r: r["revision"])
            require([r["revision"] for r in timeline] == list(range(job["revision"] + 1)), "Missing job transitions")
            for previous, following in zip(timeline, timeline[1:]):
                require(following["previous_state"] == previous["state"] and following["state"] in TRANSITIONS[previous["state"]], "Invalid persisted transition")
            initial, final = load_json(run_dir / "initial.json"), observe_database(run_dir / "business.sqlite")
            require(final == load_json(run_dir / "final.json") and final["run_id"] == initial["run_id"] == manifest["run_id"], "Independent SQLite identity/state mismatch")
            scope = manifest["case"]["context"]
            selected = lambda rows: [r for r in rows if all(r[k] == v for k, v in scope.items())]
            effects = selected(final["tickets"])
            require(not selected(initial["tickets"]) and len(effects) == count, "Unexpected initial/final business effects")
            require([r for r in final["tickets"] if r not in effects] == initial["tickets"], "Unrelated business state changed")
            require(load_json(run_dir / "http-cleanup.json")["exited"], "Ticket service did not exit")
            served = [json.loads(line) for line in (run_dir / "http-service-events.jsonl").read_text(encoding="utf8").splitlines()]
            served = [r for r in served if r["event"] == "request_received" and r["path"] == "/tickets"]
            sent = []
            for step in record["steps"]:
                processes += 1
                result_path = rebound(step["result_path"])
                result = load_json(result_path)
                require(result == step["result"] and result["pid"] == step["pid"] != step["parent_pid"] and step["exited"], "Agent process/result binding mismatch")
                require(step["exit_code"] == {"PASS": 0, "INCONCLUSIVE": 2, "ERROR": 3}[result["status"]] and result["model_requests"] == 0, "Agent exit status or model count mismatch")
                if "values" in result:
                    saved = checkpoint_values(database, manifest["thread_id"], result["checkpoint_id"])
                    require(saved == result["values"] and all(saved[k] == manifest[k] for k in IDENTITY_KEYS), "Agent state differs from PostgreSQL checkpoint blobs")
                client = result_path.parent / "http-client.jsonl"
                if client.exists():
                    rows = [json.loads(line) for line in client.read_text(encoding="utf8").splitlines()]
                    started = [r for r in rows if r["event"] == "http_request_started"]
                    require(all(r["pid"] == step["pid"] for r in started), "Wrong network process identity")
                    sent += started
            correlation = lambda records: sorted((r["request_id"], r["attempt_id"], r["method"]) for r in records)
            require(correlation(sent) == correlation(served), "Client/service requests cannot be correlated")
            require(sum(r["method"] == "POST" for r in sent) == count, "Unexpected side-effect replay")
            if name == "normal":
                steps = {r["label"]: r for r in record["steps"]}
                require(steps["start"]["pid"] != steps["read"]["pid"] and steps["start"]["result"]["values"] == steps["read"]["result"]["values"], "New process did not read the saved state")
                final_state = checkpoint_values(database, manifest["thread_id"], job["checkpoint_id"])
                require(final_state["phase"] == "completed" and final_state["ticket"] == effects[0] and final_state["create_calls"] == final_state["query_calls"] == 1, "Completed graph disagrees with business state")
            if name == "gap":
                state = checkpoint_values(database, manifest["thread_id"], job["checkpoint_id"])
                require(state["phase"] == "prepared" and state["ticket"] is None and record["steps"][-1]["result"]["status"] == "ERROR", "Unsaved side effect was treated as safe progress")
            states.append(target_state)
        return dict(status="PASS", experiments=3, agent_processes=processes, states=states, model_requests=0,
                    reason="Saved PostgreSQL checkpoints, process identities, HTTP effects and SQLite state agree")
    except Exception as error:
        return dict(status="ERROR", reason=type(error).__name__ + ": " + str(error))

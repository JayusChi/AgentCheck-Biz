"""Small, object-independent contracts for the business run lifecycle."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from typing import Protocol

from agentcheck_biz.events import EventLog


class PluginConfigurationError(ValueError):
    pass


class ObservationError(RuntimeError):
    def __init__(self, message: str, status: str = "ERROR"):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RunContext:
    run_id: str
    operation_id: str
    attempt_id: str
    deadline: float  # Absolute Unix time; never renewed by a retry.
    evidence_dir: Path

    def __post_init__(self):
        for name in ("run_id", "operation_id", "attempt_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be nonempty")
        if type(self.deadline) not in (int, float) or not math.isfinite(self.deadline):
            raise ValueError("deadline must be a finite absolute timestamp")
        object.__setattr__(self, "evidence_dir", Path(self.evidence_dir).resolve())
        if self.evidence_dir.name != self.run_id:
            raise ValueError("evidence_dir must identify this run")

    def require_time(self):
        if time.time() >= self.deadline:
            raise TimeoutError("Run deadline exhausted")

    def to_dict(self):
        return {**asdict(self), "evidence_dir": str(self.evidence_dir)}


@dataclass(frozen=True)
class Observation:
    source: str
    collected_at: str
    run_id: str
    operation_id: str
    attempt_id: str
    complete: bool
    data: object = None
    error: str | None = None
    failure_status: str = "ERROR"
    schema_version: int = 1

    @classmethod
    def failure(cls, context: RunContext, source: str, error: Exception):
        return cls(source, datetime.now(timezone.utc).isoformat(), context.run_id,
                   context.operation_id, context.attempt_id, False,
                   error=f"{type(error).__name__}: {error}")

    def to_dict(self):
        return asdict(self)

    def require_complete(self, context: RunContext):
        if self.schema_version != 1 or type(self.schema_version) is not int:
            raise ObservationError("Unsupported Observation schema_version")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ObservationError("Observation source is missing")
        try:
            if datetime.fromisoformat(self.collected_at).utcoffset() is None:
                raise ValueError("Timestamp needs a timezone")
            json.dumps(self.to_dict(), allow_nan=False)
        except (ValueError, TypeError) as error:
            raise ObservationError("Malformed observation metadata or data") from error
        if (self.run_id, self.operation_id, self.attempt_id) != (context.run_id, context.operation_id, context.attempt_id):
            raise ObservationError("Observation run_id / operation_id / attempt_id mismatch")
        if type(self.complete) is not bool or self.failure_status not in {"ERROR", "INCONCLUSIVE"}:
            raise ObservationError("Invalid observation completeness or failure status")
        if not self.complete:
            if not isinstance(self.error, str) or not self.error.strip():
                raise ObservationError("Incomplete observation must retain its failure reason")
            raise ObservationError(self.error, self.failure_status)
        if self.error is not None or self.data is None:
            raise ObservationError("Complete observation needs data and no error")


class EnvironmentAdapter(Protocol):
    def prepare(self, context: RunContext, events: EventLog) -> None: ...
    def cleanup(self, context: RunContext, events: EventLog) -> None: ...


class ExecutionAdapter(Protocol):
    def execute(self, context: RunContext, events: EventLog) -> object: ...
    def metadata(self) -> dict: ...


class StateObserver(Protocol):
    def observe(self, context: RunContext) -> Observation: ...


class BusinessVerifier(Protocol):
    def verify(self, context: RunContext, observation: Observation) -> dict: ...


@dataclass
class BusinessPlugin:
    name: str
    version: int
    case: dict
    operation_id: str
    timeout_seconds: float
    run_prefix: str
    run_metadata: dict
    environment: EnvironmentAdapter
    execution: ExecutionAdapter
    observer: StateObserver
    verifier: BusinessVerifier

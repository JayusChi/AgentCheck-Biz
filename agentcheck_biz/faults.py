"""One-shot result loss at the local tool boundary, not a network proxy."""

from dataclasses import dataclass, field


class OutcomeUnknown(RuntimeError):
    """The caller cannot infer whether its business request completed."""


class TemporaryUnavailable(RuntimeError):
    """The service was not entered; a bounded retry is allowed."""


class PermissionDenied(RuntimeError):
    """A permanent rejection: retrying the same request is not appropriate."""


@dataclass
class ResponseLossOnce:
    occurrence: int = 1
    tool: str = "create_ticket"
    point: str = "after_commit_before_result"
    kind: str = "F1"
    triggered: bool = field(default=False, init=False)
    matched_calls: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if type(self.occurrence) is not int or self.occurrence < 1:
            raise ValueError("occurrence must be a positive integer")
        if self.point != "after_commit_before_result":
            raise ValueError("D3 only supports after_commit_before_result")

    def should_trigger(self, tool: str, point: str, call_occurrence: int | None = None) -> bool:
        if tool != self.tool or point != self.point:
            return False
        self.matched_calls = call_occurrence if call_occurrence is not None else self.matched_calls + 1
        if not self.triggered and self.matched_calls == self.occurrence:
            self.triggered = True
            return True
        return False


@dataclass
class BeforeServiceFault:
    kind: str
    tool: str = "create_ticket"
    point: str = "before_service"
    occurrence: int = 1
    max_injections: int | None = 1
    trigger_count: int = field(default=0, init=False)
    matched_calls: int = field(default=0, init=False)

    def __post_init__(self):
        if self.kind not in {"F2", "F3"} or self.point != "before_service":
            raise ValueError("Only F2/F3 are supported before service entry")
        if type(self.occurrence) is not int or self.occurrence < 1:
            raise ValueError("occurrence must be a positive integer")
        if self.kind == "F2" and (self.max_injections not in (1, None) or (self.max_injections is None and self.occurrence != 1)):
            raise ValueError("F2 supports one shot or persistent failure starting at call 1")
        if self.kind == "F3" and (self.occurrence != 1 or self.max_injections is not None):
            raise ValueError("F3 rejects every matching call")

    @property
    def triggered(self) -> bool:
        return self.trigger_count > 0

    def should_trigger(self, tool: str, point: str, call_occurrence: int | None = None) -> bool:
        if tool != self.tool or point != self.point:
            return False
        self.matched_calls = call_occurrence if call_occurrence is not None else self.matched_calls + 1
        hit = (self.max_injections is None or self.matched_calls == self.occurrence) and (
            self.max_injections is None or self.trigger_count < self.max_injections)
        if hit:
            self.trigger_count += 1
        return hit


def build_fault(config: dict | None) -> ResponseLossOnce | BeforeServiceFault | None:
    if config is None:
        return None
    options = dict(config)
    kind = options.pop("kind", "F1")
    if kind in {"F1", "missing_id"}:
        if options.pop("max_injections", 1) != 1:
            raise ValueError("F1 is a one-shot result loss")
        return ResponseLossOnce(kind=kind, **options)
    return BeforeServiceFault(kind=kind, **options)

"""Append-only evidence for the serial, local D3 experiment."""

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock


class EventLog:
    def __init__(self, path: Path, run_id: str):
        self._lock = Lock()
        self.path = path
        self.run_id = run_id
        self.items: list[dict] = []
        # A run must never append evidence to an earlier run's file.
        path.touch(exist_ok=False)

    def record(self, event: str, **details) -> dict:
        with self._lock:
            return self._record(event, **details)

    def _record(self, event: str, **details) -> dict:
        item = {
            "seq": len(self.items) + 1,
            "run_id": self.run_id,
            "event": event,
            "time_utc": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        self.items.append(item)
        return item

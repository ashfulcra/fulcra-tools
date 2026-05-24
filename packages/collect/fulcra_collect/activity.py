"""In-memory ring buffer of recent annotations the daemon has written
(or attempted to write) to Fulcra. Powers the web UI's dashboard
"Recently" feed so the user sees real receipts of the app working.

The buffer holds the last 200 entries. Lost on daemon restart;
persistence to disk (sqlite) is a v1.5 if users want history beyond
the current daemon uptime.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone


BUFFER_SIZE = 200


@dataclass(frozen=True)
class ActivityEntry:
    timestamp: str       # ISO8601 with Z
    plugin_id: str
    summary: str         # human-readable (e.g. "Watched: Better Call Saul S6E13")
    ok: bool             # True if the write succeeded; False if it errored


class RecentActivity:
    def __init__(self, max_entries: int = BUFFER_SIZE) -> None:
        self._buf: deque[ActivityEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def add(self, *, plugin_id: str, summary: str, ok: bool = True,
            timestamp: datetime | None = None) -> None:
        ts = (timestamp or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
        entry = ActivityEntry(timestamp=ts, plugin_id=plugin_id,
                              summary=summary, ok=ok)
        with self._lock:
            self._buf.append(entry)

    def recent(self, limit: int = 50) -> list[ActivityEntry]:
        with self._lock:
            entries = list(self._buf)
        return entries[-limit:][::-1]  # newest first

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


def make_singleton() -> RecentActivity:
    """Return a new RecentActivity instance. The daemon constructs one in
    __init__ and passes references where needed. Called 'singleton' because
    one instance is shared across the whole daemon process."""
    return RecentActivity()

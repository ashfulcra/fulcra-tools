"""In-memory status snapshot + diff/observer protocol.

This is a pure-Python module — no PyObjC. The view layer observes it;
the polling layer feeds it. Diffing here means the UI only redraws on
actual change, and failure-threshold transitions fire exactly once per
crossing.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OverallState(Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    RUNNING = "running"
    FAILING = "failing"
    DAEMON_STOPPED = "daemon_stopped"


@dataclass
class PluginSnapshot:
    id: str
    name: str
    kind: str
    enabled: bool
    last_run: str | None
    last_outcome: str | None
    last_error: str | None
    consecutive_failures: int

    @classmethod
    def from_dict(cls, d: dict) -> "PluginSnapshot":
        return cls(
            id=d["id"], name=d["name"], kind=d["kind"],
            enabled=d.get("enabled", False),
            last_run=d.get("last_run"),
            last_outcome=d.get("last_outcome"),
            last_error=d.get("last_error"),
            consecutive_failures=d.get("consecutive_failures", 0),
        )


@dataclass
class StatusModel:
    plugins: list[PluginSnapshot] = field(default_factory=list)
    load_errors: dict[str, str] = field(default_factory=dict)
    in_flight: set[str] = field(default_factory=set)
    daemon_stopped: bool = False

    _last_snapshot_raw: Any = None
    _observers: list[Callable[["StatusModel"], None]] = field(default_factory=list)
    _failure_observers: list[Callable[[str], None]] = field(default_factory=list)
    _known_failing: set[str] = field(default_factory=set)

    def add_observer(self, fn: Callable[["StatusModel"], None]) -> None:
        self._observers.append(fn)

    def add_failure_transition_observer(self, fn: Callable[[str], None]) -> None:
        self._failure_observers.append(fn)

    def update_from_status(self, reply: dict) -> None:
        if reply == self._last_snapshot_raw and not self.daemon_stopped:
            return
        self._last_snapshot_raw = reply
        self.daemon_stopped = False
        self.plugins = [PluginSnapshot.from_dict(p) for p in reply.get("plugins", [])]
        self.load_errors = dict(reply.get("load_errors", {}))
        self._reconcile_in_flight()
        self._fire_failure_transitions()
        self._notify()

    def mark_daemon_stopped(self) -> None:
        if self.daemon_stopped:
            return
        self.daemon_stopped = True
        self._notify()

    def mark_in_flight(self, plugin_id: str) -> None:
        if plugin_id not in self.in_flight:
            self.in_flight.add(plugin_id)
            self._notify()

    @property
    def overall(self) -> OverallState:
        if self.daemon_stopped:
            return OverallState.DAEMON_STOPPED
        if not self.plugins:
            return OverallState.UNKNOWN
        if self.in_flight:
            return OverallState.RUNNING
        if any(p.consecutive_failures > 0 for p in self.plugins if p.enabled):
            return OverallState.FAILING
        return OverallState.HEALTHY

    @property
    def failing_count(self) -> int:
        return sum(1 for p in self.plugins if p.enabled and p.consecutive_failures > 0)

    def _reconcile_in_flight(self) -> None:
        """A plugin id leaves in_flight once its snapshot's last_run
        moves past the value we observed when the run was triggered.
        Here we approximate: if a plugin is in_flight AND its current
        snapshot has last_run set, treat the run as completed."""
        completed = set()
        for p in self.plugins:
            if p.id in self.in_flight and p.last_run:
                completed.add(p.id)
        self.in_flight -= completed

    def _fire_failure_transitions(self) -> None:
        now_failing = {p.id for p in self.plugins
                        if p.enabled and p.consecutive_failures >= 3}
        crossings = now_failing - self._known_failing
        self._known_failing = now_failing
        for pid in sorted(crossings):
            for fn in self._failure_observers:
                fn(pid)

    def _notify(self) -> None:
        for fn in self._observers:
            fn(self)

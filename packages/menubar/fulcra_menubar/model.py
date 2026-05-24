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
    default_interval_s: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "PluginSnapshot":
        return cls(
            id=d["id"], name=d["name"], kind=d["kind"],
            enabled=d.get("enabled", False),
            last_run=d.get("last_run"),
            last_outcome=d.get("last_outcome"),
            last_error=d.get("last_error"),
            consecutive_failures=d.get("consecutive_failures", 0),
            default_interval_s=d.get("default_interval_s"),
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
    # Maps plugin_id → the last_run value observed at the moment mark_in_flight
    # was called. _reconcile_in_flight only clears in_flight when the snapshot's
    # last_run has advanced past this baseline, not merely when it is non-null.
    _in_flight_baseline: dict[str, str | None] = field(default_factory=dict)

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
            # Capture the last_run we saw at trigger time. Only release this
            # id from in_flight when the snapshot's last_run differs (i.e. a
            # new run has actually completed and the timestamp advanced).
            current = next(
                (p.last_run for p in self.plugins if p.id == plugin_id), None
            )
            self._in_flight_baseline[plugin_id] = current
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
        has advanced past the value we captured at mark_in_flight time.
        This prevents the pulse from clearing immediately for any plugin
        that already had a non-null last_run before the run was triggered."""
        completed = set()
        for p in self.plugins:
            if p.id in self.in_flight:
                baseline = self._in_flight_baseline.get(p.id)
                # Only consider completed when last_run has actually advanced.
                if p.last_run is not None and p.last_run != baseline:
                    completed.add(p.id)
        for pid in completed:
            self.in_flight.discard(pid)
            self._in_flight_baseline.pop(pid, None)

    def _fire_failure_transitions(self) -> None:
        """Fire observers for every plugin that just crossed into >=3 failures.

        Because _known_failing is overwritten with the current failing set on
        every call, a plugin that recovers (consecutive_failures drops below 3)
        is removed from _known_failing. If it subsequently re-fails, the next
        call treats it as a new crossing and re-fires — intentionally. The user
        who received an earlier alert wants to know about the new failure even
        though they were notified about the previous one.
        """
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

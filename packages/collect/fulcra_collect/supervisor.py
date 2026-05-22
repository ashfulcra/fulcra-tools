"""Service supervision — the restart decision for a service plugin whose
worker subprocess has exited.

The decision is pure: given the timestamps of recent exits within a
window, decide whether to restart (and after what backoff) or to declare
a crash loop and stop. The daemon owns the actual subprocesses and the
sleeping; this module owns only the policy.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

CRASH_WINDOW = timedelta(seconds=60)
CRASH_LOOP_THRESHOLD = 6   # exits within CRASH_WINDOW -> degraded
BASE_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0


@dataclass
class RestartDecision:
    should_restart: bool
    backoff_seconds: float
    degraded: bool


def decide_restart(recent_exits: list[datetime], now: datetime) -> RestartDecision:
    """Decide what to do after a service worker exit. `recent_exits` is the
    exit timestamps so far (most recent last), including the one that just
    happened."""
    in_window = [t for t in recent_exits if now - t <= CRASH_WINDOW]
    if len(in_window) >= CRASH_LOOP_THRESHOLD:
        return RestartDecision(should_restart=False, backoff_seconds=0.0,
                               degraded=True)
    backoff = min(BASE_BACKOFF_S * 2 ** (len(in_window) - 1), MAX_BACKOFF_S)
    return RestartDecision(should_restart=True, backoff_seconds=backoff,
                           degraded=False)


def _terminate(proc: object) -> None:
    """Best-effort terminate of a service worker process."""
    try:
        proc.terminate()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — cleanup must never raise
        pass


class ServiceSupervisor:
    """Keeps service-plugin worker subprocesses alive.

    Stateful: tracks each service's current process and its recent exit
    times. `tick` is called by the daemon loop each tick; `spawn` is
    injected (so it is testable without real subprocesses). A process
    object need only support `.poll()` (None while running) and
    `.terminate()`.
    """

    def __init__(self) -> None:
        self._procs: dict[str, object] = {}
        self._exits: dict[str, list[datetime]] = {}
        self._backoff_until: dict[str, datetime] = {}
        self.degraded: set[str] = set()

    def _record_exit(self, pid: str, now: datetime) -> bool:
        """Record one exit for `pid` at `now`. Returns True if the service
        should be restarted, False if it has entered a crash loop (and is
        marked degraded)."""
        self._exits.setdefault(pid, []).append(now)
        decision = decide_restart(self._exits[pid], now)
        if not decision.should_restart:
            self.degraded.add(pid)
            return False
        self._backoff_until[pid] = now + timedelta(seconds=decision.backoff_seconds)
        return True

    def tick(self, *, now: datetime, enabled_ids: set[str],
             spawn: Callable[[str], object]) -> None:
        """Bring the running service set in line with `enabled_ids`:
        spawn the missing, leave the healthy, restart the exited (after a
        backoff), and stop a crash-looping one (marking it degraded).

        Each tick while a service is in its restart backoff window counts
        as an additional exit for crash-loop-detection purposes — rapid
        repeated crashes that keep the backoff alive are treated exactly
        the same as rapid process exits."""
        # Terminate services that are no longer enabled.
        for pid in list(self._procs):
            if pid not in enabled_ids:
                _terminate(self._procs.pop(pid))
        for pid in sorted(enabled_ids):
            if pid in self.degraded:
                continue
            proc = self._procs.get(pid)
            if proc is not None:
                if proc.poll() is None:
                    continue  # still running
                # Exited since the last tick.
                self._procs.pop(pid, None)
                self._record_exit(pid, now)
                # Never spawn in the same tick that observed the exit —
                # always defer at least one tick (the freshly computed
                # backoff guarantees this since backoff_seconds >= 1).
                continue
            # No proc running.  Either first start or waiting out a backoff.
            if now < self._backoff_until.get(pid, now):
                # Still in backoff window: count this idle tick toward the
                # crash-loop threshold so a service that crash-loops within
                # its own backoff (never living long enough to clear it) is
                # still detected and marked degraded.
                self._record_exit(pid, now)
            else:
                self._procs[pid] = spawn(pid)

    def shutdown_all(self) -> None:
        """Terminate every supervised process (called on daemon shutdown)."""
        for proc in list(self._procs.values()):
            _terminate(proc)
        self._procs.clear()

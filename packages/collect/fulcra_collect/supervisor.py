"""Service supervision — the restart decision for a service plugin whose
worker subprocess has exited.

The decision is pure: given the timestamps of recent exits within a
window, decide whether to restart (and after what backoff) or to declare
a crash loop and stop. The daemon owns the actual subprocesses and the
sleeping; this module owns only the policy.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

CRASH_WINDOW = timedelta(seconds=60)
CRASH_LOOP_THRESHOLD = 6   # exits within CRASH_WINDOW -> degraded
BASE_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
# A degraded service gets one more chance once its most recent crash is
# this old — a transient failure recovers; a real one re-degrades.
DEGRADED_RECOVERY_COOLDOWN = timedelta(minutes=5)
STOP_GRACE_S = 10.0


@dataclass
class RestartDecision:
    should_restart: bool
    backoff_seconds: float
    degraded: bool


@dataclass(frozen=True)
class ServiceExit:
    """One observed exit of the exact owned process, before replacement."""

    plugin_id: str
    process: object
    revision: str | None
    observed_at: datetime
    returncode: int


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
    pid = getattr(proc, "pid", None)
    if os.name == "posix" and isinstance(pid, int):
        try:
            os.killpg(pid, signal.SIGTERM)
            return
        except ProcessLookupError:
            return
        except OSError:
            # A process that was not started as a session leader has no
            # matching group. Fall back to terminating the direct child.
            pass
    try:
        proc.terminate()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — cleanup must never raise
        pass


def _kill(proc: object) -> None:
    """Force-stop an owned worker that did not honor its termination request."""
    pid = getattr(proc, "pid", None)
    if os.name == "posix" and isinstance(pid, int):
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        proc.kill()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — retain ownership and keep waiting for poll
        logging.getLogger("fulcra_collect.supervisor").error("owned service worker could not be killed")


class ServiceSupervisor:
    """Keeps service-plugin worker subprocesses alive.

    Stateful: tracks each service's current process and its recent exit
    times. `tick` is called by the daemon loop each tick; `spawn` is
    injected (so it is testable without real subprocesses). A process
    object need only support `.poll()` (None while running) and
    `.terminate()`.
    """

    def __init__(self, *, clock=time.monotonic) -> None:
        self._procs: dict[str, object] = {}
        self._exits: dict[str, list[datetime]] = {}
        self._backoff_until: dict[str, datetime] = {}
        self.degraded: set[str] = set()
        self._revisions: dict[str, str | None] = {}
        self._stopping: dict[str, float] = {}
        self._kill_sent: set[str] = set()
        self._clock = clock
        # One observer attempt per process. An uncertain commit must never be
        # retried as another increment. Retain the earliest uncertainty within
        # each revision; another launch is not evidence that status recovered.
        self.unknown_exits: dict[str, ServiceExit] = {}

    def _spawn(
        self,
        plugin_id: str,
        *,
        now: datetime,
        spawn: Callable[[str], object],
        on_spawn_error: Callable[[str, OSError], None] | None,
        revision: str | None = None,
    ) -> None:
        """Start one worker or fold an OS launch error into crash policy."""
        self._revisions[plugin_id] = revision
        try:
            self._procs[plugin_id] = spawn(plugin_id)
            return
        except OSError as exc:
            if on_spawn_error is not None:
                try:
                    on_spawn_error(plugin_id, exc)
                except Exception:  # noqa: BLE001 — observer cannot kill supervision
                    logging.getLogger("fulcra_collect.supervisor").error(
                        "service launch-error status persistence UNKNOWN for plugin=%s",
                        plugin_id,
                    )

        self._exits.setdefault(plugin_id, []).append(now)
        decision = decide_restart(self._exits[plugin_id], now)
        if not decision.should_restart:
            self.degraded.add(plugin_id)
            self._backoff_until.pop(plugin_id, None)
            return
        self._backoff_until[plugin_id] = now + timedelta(
            seconds=decision.backoff_seconds,
        )

    def tick(self, *, now: datetime, enabled_ids: set[str],
             spawn: Callable[[str], object],
             revisions: dict[str, str] | None = None,
             on_spawn_error: Callable[[str, OSError], None] | None = None,
             on_exit: Callable[[ServiceExit], None] | None = None) -> None:
        """Bring the running service set in line with `enabled_ids`:
        spawn the missing, leave the healthy, restart the exited (after a
        backoff), and stop a crash-looping one (marking it degraded).

        An exit is recorded only when a tracked process is observed to
        have died — never for a tick that is merely waiting out a backoff.
        """
        if revisions is not None:
            revisions = dict(revisions)
            if any(not isinstance(revisions.get(pid), str) or not revisions[pid] for pid in enabled_ids):
                raise ValueError("every enabled service needs a known revision")
        # A revision/disable retires the old process without spending its
        # crash budget. Keep ownership until poll confirms exit; otherwise
        # a replacement could overlap the old receiver or lose its port.
        tracked = set(self._procs) | set(self._revisions) | set(self._exits) | set(self._backoff_until) | self.degraded
        for pid in tracked:
            changed = revisions is not None and pid in enabled_ids and self._revisions.get(pid) != revisions[pid]
            if pid not in enabled_ids or changed or pid in self._stopping:
                self._exits.pop(pid, None)
                self._backoff_until.pop(pid, None)
                self.degraded.discard(pid)
                proc = self._procs.get(pid)
                if proc is not None:
                    if pid not in self._stopping:
                        if proc.poll() is None:
                            _terminate(proc)
                        self._stopping[pid] = self._clock() + STOP_GRACE_S
                    if proc.poll() is None and self._clock() >= self._stopping[pid] and pid not in self._kill_sent:
                        _kill(proc)
                        self._kill_sent.add(pid)
                        logging.getLogger("fulcra_collect.supervisor").error(
                            "service plugin=%s exceeded stop grace; forced stop requested, awaiting exit", pid)
                    if proc.poll() is None:
                        continue
                    self._procs.pop(pid, None)
                self._stopping.pop(pid, None)
                self._kill_sent.discard(pid)
                self._revisions.pop(pid, None)
        for pid in sorted(enabled_ids):
            if pid in self._stopping:
                continue
            if pid in self.degraded:
                # Auto-recovery: a degraded service whose most recent crash
                # is older than the cooldown gets one more chance — clear
                # the mark and a fresh crash budget, then fall through.
                last_exit = max(self._exits.get(pid, []), default=None)
                if last_exit is not None and now - last_exit > DEGRADED_RECOVERY_COOLDOWN:
                    self.degraded.discard(pid)
                    self._exits.pop(pid, None)
                else:
                    continue
            proc = self._procs.get(pid)
            if proc is not None:
                rc = proc.poll()
                if rc is None:
                    continue  # still running
                # Observed dead since the last tick.
                self._procs.pop(pid, None)
                if rc == 0:
                    # Clean, deliberate exit — not a crash. Respawn now;
                    # do not record an exit or set a backoff. The ~30s
                    # tick rate already bounds successful respawns. A launch
                    # failure is still a crash and gets normal backoff.
                    self._spawn(
                        pid, now=now, spawn=spawn,
                        on_spawn_error=on_spawn_error,
                        revision=revisions[pid] if revisions is not None else None,
                    )
                    continue
                # Non-zero exit — a crash. Record it and apply policy.
                if on_exit is not None:
                    observation = ServiceExit(pid, proc, self._revisions.get(pid), now, rc)
                    prior = self.unknown_exits.get(pid)
                    if prior is None or prior.revision != observation.revision:
                        self.unknown_exits[pid] = observation
                    try:
                        on_exit(observation)
                    except Exception:  # noqa: BLE001 — status cannot kill supervision
                        # Exception values/tracebacks can contain credentials.
                        logging.getLogger("fulcra_collect.supervisor").error(
                            "service exit status persistence UNKNOWN for plugin=%s", pid)
                    else:
                        if self.unknown_exits.get(pid) is observation:
                            self.unknown_exits.pop(pid, None)
                self._exits.setdefault(pid, []).append(now)
                decision = decide_restart(self._exits[pid], now)
                if not decision.should_restart:
                    self.degraded.add(pid)
                    continue
                self._backoff_until[pid] = now + timedelta(
                    seconds=decision.backoff_seconds)
            if now >= self._backoff_until.get(pid, now):
                self._spawn(
                    pid, now=now, spawn=spawn,
                    on_spawn_error=on_spawn_error,
                    revision=revisions[pid] if revisions is not None else None,
                )

    def shutdown_all(self) -> None:
        """Terminate every supervised process (called on daemon shutdown)."""
        for proc in list(self._procs.values()):
            _terminate(proc)
        self._procs.clear()
        self._revisions.clear()
        self._stopping.clear()
        self._kill_sent.clear()

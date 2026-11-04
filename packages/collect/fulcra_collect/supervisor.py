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

    def _spawn(
        self,
        plugin_id: str,
        *,
        now: datetime,
        spawn: Callable[[str], object],
        on_spawn_error: Callable[[str, OSError], None] | None,
    ) -> None:
        """Start one worker or fold an OS launch error into crash policy."""
        try:
            self._procs[plugin_id] = spawn(plugin_id)
            return
        except OSError as exc:
            if on_spawn_error is not None:
                try:
                    on_spawn_error(plugin_id, exc)
                except Exception:  # noqa: BLE001 — observer cannot kill supervision
                    logging.getLogger("fulcra_collect.supervisor").exception(
                        "service launch-error observer failed for plugin=%s",
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
             on_spawn_error: Callable[[str, OSError], None] | None = None) -> None:
        """Bring the running service set in line with `enabled_ids`:
        spawn the missing, leave the healthy, restart the exited (after a
        backoff), and stop a crash-looping one (marking it degraded).

        An exit is recorded only when a tracked process is observed to
        have died — never for a tick that is merely waiting out a backoff.
        """
        # Terminate services that are no longer enabled, and wipe their
        # supervisor state — a disable -> re-enable cycle is a clean slate.
        for pid in list(self._procs):
            if pid not in enabled_ids:
                _terminate(self._procs.pop(pid))
                self._exits.pop(pid, None)
                self._backoff_until.pop(pid, None)
                self.degraded.discard(pid)
        for pid in sorted(enabled_ids):
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
                    )
                    continue
                # Non-zero exit — a crash. Record it and apply policy.
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
                )

    def shutdown_all(self) -> None:
        """Terminate every supervised process (called on daemon shutdown)."""
        for proc in list(self._procs.values()):
            _terminate(proc)
        self._procs.clear()

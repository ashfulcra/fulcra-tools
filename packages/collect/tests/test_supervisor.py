"""The supervisor — pure service restart-decision logic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fulcra_collect.supervisor import (
    DEGRADED_RECOVERY_COOLDOWN,
    ServiceSupervisor,
    decide_restart,
)

T0 = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)


def test_first_crash_restarts_with_base_backoff():
    d = decide_restart(recent_exits=[T0], now=T0)
    assert d.should_restart is True
    assert d.backoff_seconds == 1.0  # base


def test_backoff_grows_exponentially_with_repeated_crashes():
    exits = [T0 - timedelta(seconds=10), T0 - timedelta(seconds=5), T0]
    d = decide_restart(recent_exits=exits, now=T0)
    assert d.should_restart is True
    assert d.backoff_seconds == 4.0  # 1 * 2 ** (3 - 1)


def test_a_crash_loop_marks_degraded_and_stops_restarting():
    # 6 crashes inside the 60s window -> crash loop.
    exits = [T0 - timedelta(seconds=s) for s in (50, 40, 30, 20, 10, 0)]
    d = decide_restart(recent_exits=exits, now=T0)
    assert d.should_restart is False
    assert d.degraded is True


def test_old_exits_outside_the_window_do_not_count():
    # Five ancient crashes + one fresh one -> treated as a first crash.
    old = [T0 - timedelta(hours=h) for h in (5, 4, 3, 2, 1)]
    d = decide_restart(recent_exits=old + [T0], now=T0)
    assert d.should_restart is True
    assert d.degraded is False
    assert d.backoff_seconds == 1.0


class FakeProc:
    """A stand-in service process. `alive` controls what poll() reports."""
    def __init__(self) -> None:
        self.alive = True
        self.terminated = False

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.terminated = True


def test_supervisor_spawns_an_enabled_service_on_the_first_tick():
    sup = ServiceSupervisor()
    spawned: list[str] = []

    def spawn(pid):
        spawned.append(pid)
        return FakeProc()

    sup.tick(now=T0, enabled_ids={"relay"}, spawn=spawn)
    assert spawned == ["relay"]


def test_supervisor_leaves_a_running_service_alone():
    sup = ServiceSupervisor()
    procs = []

    def spawn(pid):
        p = FakeProc()
        procs.append(p)
        return p

    sup.tick(now=T0, enabled_ids={"relay"}, spawn=spawn)
    sup.tick(now=T0 + timedelta(seconds=30), enabled_ids={"relay"}, spawn=spawn)
    assert len(procs) == 1  # not respawned


def test_supervisor_restarts_an_exited_service_after_backoff():
    sup = ServiceSupervisor()
    procs = []

    def spawn(pid):
        p = FakeProc()
        procs.append(p)
        return p

    sup.tick(now=T0, enabled_ids={"relay"}, spawn=spawn)
    procs[0].alive = False  # the service crashed
    # The tick that observes the exit records it + sets a backoff — no respawn yet.
    sup.tick(now=T0 + timedelta(seconds=30), enabled_ids={"relay"}, spawn=spawn)
    assert len(procs) == 1
    # A later tick, past the backoff, respawns.
    sup.tick(now=T0 + timedelta(seconds=120), enabled_ids={"relay"}, spawn=spawn)
    assert len(procs) == 2


class DeadProc:
    """A service process that is dead the instant it is polled — i.e. it
    crashes immediately on every spawn."""
    def poll(self):
        return 1

    def terminate(self):
        pass


def test_supervisor_marks_a_crash_looping_service_degraded():
    # A service that crashes on every spawn. Ticking once per simulated
    # second, the supervisor spawns, observes the death, backs off,
    # respawns, observes again... Exits accumulate within the 60s crash
    # window until decide_restart's threshold trips and the service is
    # marked degraded and left stopped.
    sup = ServiceSupervisor()
    t = T0
    for _ in range(60):
        sup.tick(now=t, enabled_ids={"relay"}, spawn=lambda pid: DeadProc())
        if "relay" in sup.degraded:
            break
        t += timedelta(seconds=1)
    assert "relay" in sup.degraded


def test_supervisor_terminates_a_service_that_becomes_disabled():
    sup = ServiceSupervisor()
    procs = []

    def spawn(pid):
        p = FakeProc()
        procs.append(p)
        return p

    sup.tick(now=T0, enabled_ids={"relay"}, spawn=spawn)
    sup.tick(now=T0 + timedelta(seconds=30), enabled_ids=set(), spawn=spawn)
    assert procs[0].terminated is True


class CleanExitProc:
    """A service process that exits cleanly (return code 0) — a deliberate,
    correct shutdown, not a crash."""
    def poll(self):
        return 0

    def terminate(self):
        pass


def test_clean_exit_respawns_without_crash_loop_accounting():
    # A service that exits cleanly (rc 0) on every poll. A clean exit must
    # NOT be recorded as a crash: it gets respawned immediately, never
    # accumulates exit history, and never reaches the crash-loop threshold.
    sup = ServiceSupervisor()
    spawned = 0

    def spawn(pid):
        nonlocal spawned
        spawned += 1
        return CleanExitProc()

    t = T0
    for _ in range(20):
        sup.tick(now=t, enabled_ids={"relay"}, spawn=spawn)
        t += timedelta(seconds=1)

    # Respawned every tick (no backoff), never degraded, no exit history.
    assert spawned == 20
    assert "relay" not in sup.degraded
    assert sup._exits.get("relay", []) == []


def test_disable_clears_all_supervisor_state_for_the_plugin():
    # A disabled service must be a clean slate when re-enabled: no stale
    # exit history, no stale backoff, no lingering degraded membership.
    sup = ServiceSupervisor()
    sup._exits["relay"] = [T0 - timedelta(seconds=5), T0]
    sup._backoff_until["relay"] = T0 + timedelta(seconds=60)
    sup.degraded.add("relay")
    sup._procs["relay"] = FakeProc()

    # A tick with "relay" no longer enabled disables it.
    sup.tick(now=T0, enabled_ids=set(), spawn=lambda pid: FakeProc())

    assert "relay" not in sup._exits
    assert "relay" not in sup._backoff_until
    assert "relay" not in sup.degraded


def test_degraded_service_auto_recovers_after_cooldown():
    # A service degraded by a transient crash loop must get another chance:
    # once its most recent crash is older than DEGRADED_RECOVERY_COOLDOWN,
    # the next tick clears the degraded mark and respawns it.
    sup = ServiceSupervisor()
    spawned: list[str] = []

    def spawn(pid):
        spawned.append(pid)
        return FakeProc()  # this time it stays alive

    sup.degraded.add("relay")
    sup._exits["relay"] = [T0]

    # Still inside the cooldown: stays degraded, not respawned.
    sup.tick(now=T0 + DEGRADED_RECOVERY_COOLDOWN - timedelta(seconds=1),
             enabled_ids={"relay"}, spawn=spawn)
    assert "relay" in sup.degraded
    assert spawned == []

    # Past the cooldown: recovered and respawned with a fresh crash budget.
    sup.tick(now=T0 + DEGRADED_RECOVERY_COOLDOWN + timedelta(seconds=1),
             enabled_ids={"relay"}, spawn=spawn)
    assert "relay" not in sup.degraded
    assert spawned == ["relay"]
    assert sup._exits.get("relay", []) == []

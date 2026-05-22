"""The supervisor — pure service restart-decision logic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fulcra_collect.supervisor import RestartDecision, ServiceSupervisor, decide_restart

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


def test_supervisor_marks_a_crash_looping_service_degraded():
    sup = ServiceSupervisor()

    def spawn(pid):
        return FakeProc()  # every spawn immediately "dead" on next poll

    # Drive 7 ticks 1s apart; each spawned proc is dead by the next tick.
    t = T0
    for _ in range(8):
        for p in list(sup._procs.values()):
            p.alive = False
        sup.tick(now=t, enabled_ids={"relay"}, spawn=spawn)
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

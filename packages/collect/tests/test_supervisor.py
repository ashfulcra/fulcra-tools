"""The supervisor — pure service restart-decision logic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fulcra_collect.supervisor import RestartDecision, decide_restart

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

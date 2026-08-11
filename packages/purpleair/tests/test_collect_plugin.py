

# --------------------------------------------------------------------------
# Freshness opt-in. A sensor's silence is unambiguous — unlike a human-driven
# source — which is what makes this source monitorable at all.
# --------------------------------------------------------------------------


def test_the_plugin_opts_in_to_freshness_monitoring():
    """Monitoring is opt-in, so a plugin that does not declare an expectation
    is never assessed. This one must, or a dead sensor stays invisible."""
    from fulcra_purpleair.collect_plugin import PLUGIN

    assert PLUGIN.freshness is not None
    assert PLUGIN.freshness.is_configured


def test_the_bound_is_far_wider_than_the_poll_interval():
    """Guards the reasoning, not the number. A bound derived from the poll
    interval would flap on ordinary sensor reboots, Wi-Fi drops and API
    maintenance — and an alert that cries wolf gets switched off before the
    real failure arrives. It must also survive an operator widening the
    daemon interval."""
    from fulcra_purpleair.collect_plugin import DEFAULT_INTERVAL, PLUGIN

    assert PLUGIN.freshness.max_yield_silence >= DEFAULT_INTERVAL * 24


def test_a_dead_sensor_is_reported_stale():
    from datetime import datetime, timedelta, timezone

    from fulcra_collect.freshness import Freshness, assess
    from fulcra_purpleair.collect_plugin import PLUGIN

    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    report = assess(
        plugin_id=PLUGIN.id, now=now, expectation=PLUGIN.freshness,
        last_yield_at=now - timedelta(days=1),
        newest_item_at=now - timedelta(days=1),
    )
    assert report.state is Freshness.STALE


def test_an_ordinary_multi_hour_outage_is_not_stale():
    """The false-alarm direction, which matters more than the true-positive:
    a sensor back after a few hours must not have raised an alert."""
    from datetime import datetime, timedelta, timezone

    from fulcra_collect.freshness import Freshness, assess
    from fulcra_purpleair.collect_plugin import PLUGIN

    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    report = assess(
        plugin_id=PLUGIN.id, now=now, expectation=PLUGIN.freshness,
        last_yield_at=now - timedelta(hours=4),
        newest_item_at=now - timedelta(hours=4),
    )
    assert report.state is Freshness.FRESH


def test_a_backward_sensor_clock_is_caught_by_the_upstream_bound_alone():
    """The ONLY case where the two declared clocks disagree, and therefore the
    entire justification for declaring both.

    A stuck sensor clock is already caught by yield silence: the dedup key
    embeds the reading timestamp, so a repeated timestamp is claimed once and
    every later poll skips. But a clock that jumps BACKWARD (NTP correction,
    firmware reset) mints fresh dedup keys, so writes keep succeeding and the
    yield clock keeps advancing while the data is stale. Only max_upstream_lag
    sees it. Delete that bound and this test must fail.
    """
    from datetime import datetime, timedelta, timezone

    from fulcra_collect.freshness import Freshness, assess
    from fulcra_purpleair.collect_plugin import PLUGIN

    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    report = assess(
        plugin_id=PLUGIN.id, now=now, expectation=PLUGIN.freshness,
        last_yield_at=now - timedelta(minutes=5),   # still writing happily
        newest_item_at=now - timedelta(days=2),     # carrying stale readings
    )
    assert report.state is Freshness.STALE
    assert "newest item is 2d old" in report.summary

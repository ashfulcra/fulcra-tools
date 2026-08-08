"""Freshness assessment — the cases that motivated the module, written as the
failures they represent rather than as coverage of its branches."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fulcra_collect.freshness import (
    CLOCK_SKEW_TOLERANCE,
    Freshness,
    FreshnessExpectation,
    assess,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)

# A source expected to produce at least daily, whose items should never be
# much more than a day old.
DAILY = FreshnessExpectation(
    max_yield_silence=timedelta(days=1),
    max_upstream_lag=timedelta(days=1),
)


def _assess(**kw):
    kw.setdefault("plugin_id", "example")
    kw.setdefault("now", NOW)
    kw.setdefault("expectation", DAILY)
    return assess(**kw)


# --------------------------------------------------------------------------
# THE INSTANCE: the outage this module exists to have caught.
# --------------------------------------------------------------------------


def test_a_plugin_that_runs_cleanly_but_collects_nothing_is_stale():
    """The original failure: four days of clean `done` runs with zero records
    accepted, and every health signal green because they all watched the RUN.

    Note the call site — `assess` is never told the run outcome. A clean run is
    not evidence of freshness, which is precisely the confusion being removed.
    """
    report = _assess(
        last_yield_at=NOW - timedelta(days=4),
        newest_item_at=NOW - timedelta(days=4),
    )
    assert report.state is Freshness.STALE
    assert report.is_alerting
    assert "collected nothing for 4d" in report.summary


# --------------------------------------------------------------------------
# THE SIBLING: what a yield-only check would wave straight through.
# --------------------------------------------------------------------------


def test_a_plugin_still_writing_stale_items_is_stale_even_though_it_just_yielded():
    """The neighbouring failure direction. The plugin accepted a record one
    minute ago, so any "did we write recently" check calls it healthy — but
    every record is a re-emission of an item from a week ago. Upstream is dead
    and no new information is arriving.

    This is why freshness tracks two independent clocks. Delete
    `max_upstream_lag` from the expectation and this test must fail.
    """
    report = _assess(
        last_yield_at=NOW - timedelta(minutes=1),   # writing happily
        newest_item_at=NOW - timedelta(days=7),     # upstream frozen
    )
    assert report.state is Freshness.STALE
    assert "newest item is 7d old" in report.summary


def test_upstream_lag_alone_is_enough_to_alert():
    """Same failure with only the upstream bound declared — a plugin whose
    write cadence is genuinely irregular but whose data should stay current."""
    report = _assess(
        expectation=FreshnessExpectation(max_upstream_lag=timedelta(hours=6)),
        last_yield_at=NOW - timedelta(minutes=1),
        newest_item_at=NOW - timedelta(days=2),
    )
    assert report.state is Freshness.STALE


# --------------------------------------------------------------------------
# THE FALSE-ALARM DIRECTION: an alert that cries wolf is worse than none.
# --------------------------------------------------------------------------


def test_a_source_with_no_declared_expectation_never_alerts():
    """Rare-by-nature sources (a manual importer, a lab result that arrives
    every few months) must not be given a guessed bound. Silence is their
    normal state; alerting on it would train operators to ignore the alert."""
    for expectation in (None, FreshnessExpectation()):
        report = _assess(
            expectation=expectation,
            last_yield_at=NOW - timedelta(days=365),
            newest_item_at=NOW - timedelta(days=365),
        )
        assert report.state is Freshness.NOT_CONFIGURED
        assert not report.is_alerting


def test_quiet_but_within_expectation_is_fresh():
    report = _assess(
        last_yield_at=NOW - timedelta(hours=20),
        newest_item_at=NOW - timedelta(hours=20),
    )
    assert report.state is Freshness.FRESH
    assert not report.is_alerting


def test_exactly_at_the_bound_is_not_yet_stale():
    """Boundary is inclusive-fresh: a source sampled on a 24h cadence should
    not flap into alerting on the tick it is due."""
    report = _assess(
        last_yield_at=NOW - timedelta(days=1),
        newest_item_at=NOW - timedelta(days=1),
    )
    assert report.state is Freshness.FRESH


# --------------------------------------------------------------------------
# ABSENCE IS UNKNOWN — never healthy, never stale.
# --------------------------------------------------------------------------


def test_a_plugin_that_has_never_yielded_is_unknown_not_stale():
    """No evidence either way. Reporting UNKNOWN keeps "we have not looked"
    distinct from "we looked and it is fine" — the distinction whose collapse
    caused the outage."""
    report = _assess(last_yield_at=None, newest_item_at=None)
    assert report.state is Freshness.UNKNOWN
    assert not report.is_alerting
    assert "never accepted a record" in report.summary


def test_a_plugin_with_yields_but_no_source_timestamp_is_unknown():
    """Upstream age cannot be judged without a source timestamp; do not fall
    back to the yield clock, which would silently answer a different question
    than the one asked."""
    report = _assess(
        last_yield_at=NOW - timedelta(minutes=5),
        newest_item_at=None,
    )
    assert report.state is Freshness.UNKNOWN
    assert "no source timestamp" in report.summary


# --------------------------------------------------------------------------
# CLOCK SKEW: a bad clock must not manufacture permanent freshness.
# --------------------------------------------------------------------------


def test_a_future_source_timestamp_degrades_to_unknown_not_fresh():
    """A source whose clock runs ahead would otherwise produce a negative age
    that compares below every bound forever — a stalled source reading fresh
    for good. Refuse to judge instead."""
    report = _assess(
        last_yield_at=NOW - timedelta(minutes=1),
        newest_item_at=NOW + timedelta(hours=3),
    )
    assert report.state is Freshness.UNKNOWN
    assert report.clock_skew is True


def test_small_clock_differences_are_tolerated():
    """Sub-tolerance skew is normal between hosts and must not degrade an
    otherwise healthy source to UNKNOWN."""
    report = _assess(
        last_yield_at=NOW - timedelta(minutes=1),
        newest_item_at=NOW + (CLOCK_SKEW_TOLERANCE / 2),
    )
    assert report.state is Freshness.FRESH
    assert report.clock_skew is False
    assert report.upstream_age == timedelta(0)


# --------------------------------------------------------------------------
# Construction guards.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["max_yield_silence", "max_upstream_lag"])
@pytest.mark.parametrize("bad", [timedelta(0), timedelta(seconds=-1)])
def test_non_positive_bounds_are_rejected(field, bad):
    """A zero or negative bound would mark every source permanently stale."""
    with pytest.raises(ValueError, match=field):
        FreshnessExpectation(**{field: bad})


def test_naive_timestamps_are_treated_as_utc():
    """State loaded from older rows may lack tzinfo; that must not raise."""
    report = _assess(
        # Naive on purpose — that is the condition under test.
        last_yield_at=datetime(2026, 8, 8, 11, 0),  # noqa: DTZ001
        newest_item_at=datetime(2026, 8, 8, 11, 0),  # noqa: DTZ001
    )
    assert report.state is Freshness.FRESH


# --------------------------------------------------------------------------
# ROUND 2 — codex-reviewer, head 6a2b387a. Two correctness bugs, both about
# one signal wrongly overriding another.
# --------------------------------------------------------------------------


def test_a_proven_breach_is_not_erased_by_an_unknown_in_the_other_dimension():
    """Bug 2. Either bound alone is sufficient to prove staleness, so an
    absent signal in one dimension must never downgrade a breach proven in the
    other. The original code returned UNKNOWN from inside each dimension's
    branch, so a two-day-stale source with no yield clock reported UNKNOWN and
    would not have alerted."""
    report = _assess(
        last_yield_at=None,                       # unknown dimension
        newest_item_at=NOW - timedelta(days=2),   # PROVEN breach
    )
    assert report.state is Freshness.STALE, "an unknown erased a proven breach"


def test_the_mirror_case_a_stale_yield_with_no_source_timestamp():
    """Same bug, dimensions swapped."""
    report = _assess(
        last_yield_at=NOW - timedelta(days=2),    # PROVEN breach
        newest_item_at=None,                      # unknown dimension
    )
    assert report.state is Freshness.STALE


def test_clock_skew_in_one_dimension_does_not_erase_a_breach_in_the_other():
    """Skew makes THAT dimension untrustworthy — it is not a licence to ignore
    a breach the other dimension proves."""
    report = _assess(
        last_yield_at=NOW - timedelta(days=3),    # PROVEN breach
        newest_item_at=NOW + timedelta(hours=5),  # untrustworthy
    )
    assert report.state is Freshness.STALE
    assert report.clock_skew is True


def test_unknown_still_wins_when_no_breach_is_proven():
    """The fail-closed half must survive the fix: with nothing proven and a
    required signal missing, the answer is UNKNOWN, never FRESH."""
    report = _assess(last_yield_at=None, newest_item_at=NOW - timedelta(hours=1))
    assert report.state is Freshness.UNKNOWN

"""Freshness must be wired to the ACTING path, not merely computable.

A correct decision function that nothing calls is worth nothing. These tests
exercise the real entry points — `PluginState` persistence, the runner's
finish path, and the status route — so that cutting any link fails a test
rather than silently reverting the daemon to run-status-only health.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fulcra_collect import db, state
from fulcra_collect.freshness import Freshness, FreshnessExpectation, assess

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Persistence: the two watermarks must survive a round trip.
# --------------------------------------------------------------------------


def test_freshness_columns_round_trip_through_the_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "state.db", raising=False)
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))

    st = state.load("example")
    st.record_finish(outcome="done", when=NOW)
    st.record_yield(when=NOW, observed_at="2026-08-08T11:00:00+00:00")
    state.save(st)

    reloaded = state.load("example")
    assert reloaded.last_yield_at is not None
    assert reloaded.newest_item_at == "2026-08-08T11:00:00+00:00"


def test_the_schema_migration_is_registered():
    """A column added without bumping LATEST_VERSION never reaches an existing
    install — the feature would work only on fresh databases."""
    assert db.LATEST_VERSION >= 7
    assert hasattr(db, "_migration_007_freshness")


# --------------------------------------------------------------------------
# The monotonic rule: backfill must not manufacture a stall.
# --------------------------------------------------------------------------


def test_accepting_an_older_item_does_not_drag_the_watermark_backwards():
    """A backfill legitimately accepts items older than ones already held. If
    newest_item_at tracked "last accepted" rather than "newest ever", a healthy
    import would push the source into STALE — the alert firing on the very
    operation that proves the source works."""
    st = state.PluginState(plugin_id="example")
    st.record_yield(when=NOW, observed_at="2026-08-08T10:00:00+00:00")
    st.record_yield(when=NOW, observed_at="2026-01-01T00:00:00+00:00")  # backfill
    assert st.newest_item_at == "2026-08-08T10:00:00+00:00"


def test_a_yield_without_a_source_timestamp_still_advances_the_yield_clock():
    """Plugins that cannot report a source time are still monitored for total
    silence; they just cannot be monitored for a frozen upstream."""
    st = state.PluginState(plugin_id="example")
    st.record_yield(when=NOW)
    assert st.last_yield_at == NOW
    assert st.newest_item_at is None


# --------------------------------------------------------------------------
# The runner: a clean run that produced nothing must NOT refresh the clock.
# --------------------------------------------------------------------------


def test_record_finish_alone_never_touches_the_freshness_clock():
    """This is the whole mechanism in one assertion. The outage looked exactly
    like this: finish after finish with outcome="done", and nothing produced.
    If record_finish ever set last_yield_at, freshness would report FRESH for a
    dead source and the feature would be worse than useless."""
    st = state.PluginState(plugin_id="example")
    for _ in range(100):
        st.record_finish(outcome="done", when=NOW)

    assert st.last_yield_at is None
    assert st.consecutive_failures == 0          # every run "healthy"
    assert st.last_outcome == "done"

    report = assess(
        plugin_id="example",
        now=NOW,
        expectation=FreshnessExpectation(max_yield_silence=timedelta(hours=6)),
        last_yield_at=st.last_yield_at,
    )
    assert report.state is Freshness.UNKNOWN     # never yielded: not "fresh"


def test_runner_records_a_yield_only_for_accepted_receipts():
    """The runner counts ok=True annotation receipts as yields and ignores
    ok=False (attempted-but-failed writes). Guarding the source rather than
    re-implementing the subprocess loop: a failed write must not read as
    evidence the source is producing."""
    import inspect

    from fulcra_collect import runner

    src = inspect.getsource(runner)
    assert "accepted_count" in src, "runner no longer tracks accepted receipts"
    assert "record_yield" in src, "runner no longer records yields — freshness is unwired"
    # The yield must be conditional on something having been accepted; an
    # unconditional call would refresh the clock on every empty run.
    assert "if accepted_count:" in src


def test_the_status_route_reports_freshness():
    """The operator-facing surface must carry the verdict. Without this the
    signal exists in the database and is visible to nobody."""
    import inspect

    from fulcra_collect.routes import plugins as plugins_route

    src = inspect.getsource(plugins_route)
    assert '"freshness": _freshness_payload(plugin)' in src
    assert "freshness.assess(" in src


# --------------------------------------------------------------------------
# Opt-in stays opt-in through the real Plugin type.
# --------------------------------------------------------------------------


def test_plugins_are_not_monitored_unless_they_declare_an_expectation():
    from fulcra_collect.plugin import Plugin

    p = Plugin(id="example", name="Example", kind="scheduled",
               collect_mode="historical", default_interval=3600,
               run=lambda ctx: None)
    assert p.freshness is None

    report = assess(
        plugin_id=p.id, now=NOW, expectation=p.freshness,
        last_yield_at=NOW - timedelta(days=400),
    )
    assert report.state is Freshness.NOT_CONFIGURED
    assert not report.is_alerting


def test_a_plugin_can_declare_an_expectation():
    from fulcra_collect.plugin import Plugin

    p = Plugin(
        id="example", name="Example", kind="scheduled",
        collect_mode="historical", default_interval=3600,
        run=lambda ctx: None,
        freshness=FreshnessExpectation(max_yield_silence=timedelta(days=1)),
    )
    report = assess(
        plugin_id=p.id, now=NOW, expectation=p.freshness,
        last_yield_at=NOW - timedelta(days=3),
    )
    assert report.state is Freshness.STALE


# --------------------------------------------------------------------------
# ROUND 2 — codex-reviewer. Lexicographic ISO comparison is not chronological.
# --------------------------------------------------------------------------


def test_offsets_are_compared_as_instants_not_as_strings():
    """Bug 1, codex's exact probe. '2026-08-08T12:00:00+05:00' is 07:00Z —
    OLDER than '2026-08-08T08:00:00+00:00' — but sorts LATER as a string. A
    raw string comparison moves the watermark backwards in real time, which is
    precisely the monotonic guarantee record_yield exists to provide."""
    st = state.PluginState(plugin_id="example")
    st.record_yield(when=NOW, observed_at="2026-08-08T08:00:00+00:00")
    st.record_yield(when=NOW, observed_at="2026-08-08T12:00:00+05:00")  # older!

    from datetime import datetime as _dt
    kept = _dt.fromisoformat(st.newest_item_at)
    assert kept == _dt(2026, 8, 8, 8, 0, tzinfo=timezone.utc)


def test_equivalent_instants_in_different_offsets_do_not_regress():
    """The same moment written two ways must be a no-op, not a rewrite."""
    st = state.PluginState(plugin_id="example")
    st.record_yield(when=NOW, observed_at="2026-08-08T12:00:00+00:00")
    first = st.newest_item_at
    st.record_yield(when=NOW, observed_at="2026-08-08T17:00:00+05:00")  # same instant
    from datetime import datetime as _dt
    assert _dt.fromisoformat(st.newest_item_at) == _dt.fromisoformat(first)


def test_the_watermark_is_persisted_in_a_canonical_utc_form():
    """Storing whatever offset the source happened to send makes every future
    comparison depend on parsing. Normalize once, on write."""
    st = state.PluginState(plugin_id="example")
    st.record_yield(when=NOW, observed_at="2026-08-08T17:00:00+05:00")
    assert st.newest_item_at.endswith("+00:00")


def test_an_unparseable_source_timestamp_does_not_advance_the_watermark():
    """Garbage must not become the high-water mark — that would freeze the
    watermark forever and mask every real advance behind it."""
    st = state.PluginState(plugin_id="example")
    st.record_yield(when=NOW, observed_at="2026-08-08T10:00:00+00:00")
    good = st.newest_item_at
    st.record_yield(when=NOW, observed_at="not-a-timestamp")
    assert st.newest_item_at == good
    assert st.last_yield_at == NOW      # the yield itself still counts

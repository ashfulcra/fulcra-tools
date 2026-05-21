"""Tests for fulcra_csv.confidence."""
from datetime import datetime, timedelta, timezone

import pytest

from fulcra_csv import (
    ClusterPolicy,
    GenericEvent,
    apply_cluster_policy,
    apply_twin_decisions,
    cluster_size_of,
    confidence_of,
    find_low_conf_twins,
)


def _event(*, ts: datetime, source_id: str,
           confidence: str | None = None, cluster_size: int | None = None,
           fingerprint: str | None = None,
           duration_seconds: int = 1) -> GenericEvent:
    external: dict = {}
    if confidence:
        external["timestamp_confidence"] = confidence
    if cluster_size is not None:
        external["timestamp_cluster_size"] = cluster_size
    if fingerprint:
        external["content_fingerprint"] = fingerprint
    return GenericEvent(
        start_time=ts,
        end_time=ts + timedelta(seconds=duration_seconds),
        note=source_id,
        title=source_id,
        source_id=source_id,
        external_ids=external,
    )


# ---------- helpers ----------

def test_confidence_of_reads_external_ids_first():
    e = _event(ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
               source_id="a", confidence="low")
    assert confidence_of(e) == "low"


def test_confidence_of_falls_back_to_attr():
    """Some adapters (NormalizedEvent) carry confidence as a top-level attr."""
    class Fake:
        external_ids = {}
        timestamp_confidence = "medium"
    assert confidence_of(Fake()) == "medium"


def test_confidence_of_returns_none_when_absent():
    e = _event(ts=datetime(2026, 1, 1, tzinfo=timezone.utc), source_id="a")
    assert confidence_of(e) is None


def test_cluster_size_of_reads_external_ids():
    e = _event(ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
               source_id="a", cluster_size=42)
    assert cluster_size_of(e) == 42


def test_cluster_size_of_returns_zero_when_absent():
    e = _event(ts=datetime(2026, 1, 1, tzinfo=timezone.utc), source_id="a")
    assert cluster_size_of(e) == 0


# ---------- ClusterPolicy ----------

def test_cluster_policy_rejects_unknown_action():
    with pytest.raises(ValueError, match="action must be one of"):
        ClusterPolicy(action="ignore")


def test_cluster_policy_rejects_out_of_range_sentinel_year():
    with pytest.raises(ValueError, match="sentinel_year out of range"):
        ClusterPolicy(action="sentinel", sentinel_year=1900)


# ---------- apply_cluster_policy ----------

def _scenario() -> list[GenericEvent]:
    base = datetime(2026, 5, 16, 18, 0, tzinfo=timezone.utc)
    return [
        # 3 cluster members on the signup day
        _event(ts=base, source_id="cluster-1", confidence="low", cluster_size=2910,
               duration_seconds=300),
        _event(ts=base + timedelta(seconds=1), source_id="cluster-2",
               confidence="low", cluster_size=2910, duration_seconds=180),
        _event(ts=base + timedelta(seconds=2), source_id="cluster-3",
               confidence="low", cluster_size=2910),
        # 2 clean events
        _event(ts=datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc),
               source_id="clean-1", confidence="high"),
        _event(ts=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
               source_id="clean-2", confidence="high"),
    ]


def test_apply_cluster_policy_keep_is_identity():
    events = _scenario()
    out = apply_cluster_policy(events, ClusterPolicy(action="keep"))
    assert out == events  # same objects, in order


def test_apply_cluster_policy_drop_removes_cluster_members():
    out = apply_cluster_policy(_scenario(), ClusterPolicy(action="drop"))
    assert [e.source_id for e in out] == ["clean-1", "clean-2"]


def test_apply_cluster_policy_drop_respects_threshold():
    """Events with cluster_size < threshold are left alone."""
    events = [
        _event(ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
               source_id="small-cluster", cluster_size=3),  # below threshold of 5
        _event(ts=datetime(2026, 1, 2, tzinfo=timezone.utc),
               source_id="big-cluster", cluster_size=100),
    ]
    out = apply_cluster_policy(events, ClusterPolicy(action="drop"))
    assert [e.source_id for e in out] == ["small-cluster"]


def test_apply_cluster_policy_sentinel_shifts_clusters_to_year():
    out = apply_cluster_policy(
        _scenario(),
        ClusterPolicy(action="sentinel", sentinel_year=2010),
    )
    # 3 cluster + 2 clean events still present, none dropped
    assert len(out) == 5
    sentinel_events = [e for e in out if "cluster" in e.source_id]
    for e in sentinel_events:
        assert e.start_time.year == 2010
        assert e.start_time.month == 1
        assert e.start_time.day == 1
        assert e.external_ids["sentinel_applied"] is True
        assert "original_timestamp" in e.external_ids


def test_apply_cluster_policy_sentinel_preserves_duration():
    out = apply_cluster_policy(
        _scenario(),
        ClusterPolicy(action="sentinel", sentinel_year=2010),
    )
    cluster_1 = next(e for e in out if e.source_id == "cluster-1")
    # cluster-1 had a 5-minute duration in _scenario
    assert (cluster_1.end_time - cluster_1.start_time).total_seconds() == 300


def test_apply_cluster_policy_sentinel_orders_by_original_time():
    """1ms gaps preserve original ordering."""
    base = datetime(2026, 5, 16, 18, 0, tzinfo=timezone.utc)
    # Deliberately scramble cluster events in input order; ensure
    # sentinel-mapped output is sorted by original timestamp.
    events = [
        _event(ts=base + timedelta(seconds=5), source_id="c-late",
               cluster_size=10),
        _event(ts=base, source_id="c-early", cluster_size=10),
        _event(ts=base + timedelta(seconds=2), source_id="c-mid",
               cluster_size=10),
    ]
    out = apply_cluster_policy(
        events, ClusterPolicy(action="sentinel", sentinel_year=2010))
    by_time = sorted(out, key=lambda e: e.start_time)
    assert [e.source_id for e in by_time] == ["c-early", "c-mid", "c-late"]


def test_apply_cluster_policy_sentinel_doesnt_recompute_source_id():
    """source_id was hashed against the original timestamp — keeping it
    intact means re-running the same input gives the same source_ids."""
    out = apply_cluster_policy(
        _scenario(),
        ClusterPolicy(action="sentinel", sentinel_year=2010),
    )
    cluster_ids = [e.source_id for e in out if "cluster" in e.source_id]
    assert sorted(cluster_ids) == ["cluster-1", "cluster-2", "cluster-3"]


# ---------- find_low_conf_twins ----------

def test_find_low_conf_twins_pairs_by_content_fingerprint():
    events = [
        _event(ts=datetime(2026, 5, 16, 18, tzinfo=timezone.utc),
               source_id="trakt-low", confidence="low",
               fingerprint="tv:dune:s01e01"),
        _event(ts=datetime(2026, 4, 1, 20, tzinfo=timezone.utc),
               source_id="netflix-high", confidence="high",
               fingerprint="tv:dune:s01e01"),
        # No twin in this set
        _event(ts=datetime(2026, 5, 16, 18, tzinfo=timezone.utc),
               source_id="trakt-orphan", confidence="low",
               fingerprint="tv:other-show:s01e01"),
    ]
    pairs = find_low_conf_twins(events)
    assert len(pairs) == 1
    low, high = pairs[0]
    assert low.source_id == "trakt-low"
    assert high.source_id == "netflix-high"


def test_find_low_conf_twins_uses_extra_pool():
    """Cross-batch twin detection works when caller provides a cache."""
    new = [
        _event(ts=datetime(2026, 5, 16, 18, tzinfo=timezone.utc),
               source_id="incoming-low", confidence="low",
               fingerprint="music:phoenix:1901"),
    ]
    cached = [
        _event(ts=datetime(2026, 4, 1, 12, tzinfo=timezone.utc),
               source_id="prev-high", confidence="high",
               fingerprint="music:phoenix:1901"),
    ]
    pairs = find_low_conf_twins(new, extra_pool=cached)
    assert len(pairs) == 1
    assert pairs[0][0].source_id == "incoming-low"
    assert pairs[0][1].source_id == "prev-high"


def test_find_low_conf_twins_ignores_pairs_without_fingerprint():
    events = [
        _event(ts=datetime(2026, 5, 16, 18, tzinfo=timezone.utc),
               source_id="low-no-fp", confidence="low"),
        _event(ts=datetime(2026, 4, 1, 20, tzinfo=timezone.utc),
               source_id="high-no-fp", confidence="high"),
    ]
    assert find_low_conf_twins(events) == []


def test_find_low_conf_twins_only_low_to_high():
    """Two high-conf events with same fingerprint aren't paired."""
    events = [
        _event(ts=datetime(2026, 4, 1, 20, tzinfo=timezone.utc),
               source_id="hc-1", confidence="high", fingerprint="x"),
        _event(ts=datetime(2026, 4, 2, 20, tzinfo=timezone.utc),
               source_id="hc-2", confidence="high", fingerprint="x"),
    ]
    assert find_low_conf_twins(events) == []


# ---------- apply_twin_decisions ----------

def test_apply_twin_decisions_filters_listed_ids():
    events = [
        _event(ts=datetime(2026, 1, 1, tzinfo=timezone.utc), source_id="keep"),
        _event(ts=datetime(2026, 1, 2, tzinfo=timezone.utc), source_id="discard"),
        _event(ts=datetime(2026, 1, 3, tzinfo=timezone.utc), source_id="also-keep"),
    ]
    out = apply_twin_decisions(events, {"discard"})
    assert [e.source_id for e in out] == ["keep", "also-keep"]

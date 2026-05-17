"""Verify fulcra_csv.confidence works on fulcra_media.NormalizedEvent.

NormalizedEvent has __post_init__ validation (category, confidence, tz);
dataclasses.replace() re-runs that. Make sure the sentinel-shifted events
still pass validation.
"""
from datetime import datetime, timedelta, timezone

from fulcra_csv import ClusterPolicy, apply_cluster_policy, cluster_size_of, confidence_of

from fulcra_media.importers.base import NormalizedEvent


def _norm(*, ts: datetime, sid: str, conf: str = "low", cluster_size: int = 100,
          dur: int = 30) -> NormalizedEvent:
    return NormalizedEvent(
        importer="trakt",
        service="trakt",
        category="watched",
        note=f"note {sid}",
        title=f"title {sid}",
        start_time=ts,
        end_time=ts + timedelta(seconds=dur),
        deterministic_id=sid,
        timestamp_confidence=conf,
        external_ids={"timestamp_cluster_size": cluster_size, "content_fingerprint": "fp"},
    )


def test_confidence_of_reads_normalizedevent_top_level_attr():
    """NormalizedEvent stores confidence top-level, not in external_ids."""
    e = _norm(ts=datetime(2026, 5, 16, 18, tzinfo=timezone.utc), sid="x", conf="low")
    assert confidence_of(e) == "low"


def test_cluster_size_of_reads_normalizedevent_external_ids():
    e = _norm(ts=datetime(2026, 5, 16, 18, tzinfo=timezone.utc), sid="x", cluster_size=2910)
    assert cluster_size_of(e) == 2910


def test_apply_cluster_policy_sentinel_on_normalizedevent():
    """Critical: replace() on NormalizedEvent re-runs __post_init__ validation."""
    base = datetime(2026, 5, 16, 18, tzinfo=timezone.utc)
    events = [
        _norm(ts=base, sid="cluster-a"),
        _norm(ts=base + timedelta(seconds=1), sid="cluster-b"),
        _norm(ts=datetime(2026, 4, 1, 12, tzinfo=timezone.utc),
              sid="clean", conf="high", cluster_size=0),
    ]
    out = apply_cluster_policy(events, ClusterPolicy(action="sentinel", sentinel_year=2015))
    assert len(out) == 3
    shifted = [e for e in out if e.deterministic_id.startswith("cluster-")]
    assert all(e.start_time.year == 2015 for e in shifted)
    # NormalizedEvent's tz-aware validation passes
    assert all(e.start_time.tzinfo is not None for e in shifted)
    # Duration preserved
    assert all((e.end_time - e.start_time).total_seconds() == 30 for e in shifted)
    # Confidence value still "low" (sentinel doesn't change confidence)
    assert all(e.timestamp_confidence == "low" for e in shifted)


def test_apply_cluster_policy_drop_on_normalizedevent():
    base = datetime(2026, 5, 16, 18, tzinfo=timezone.utc)
    events = [
        _norm(ts=base, sid="cluster"),
        _norm(ts=datetime(2026, 4, 1, 12, tzinfo=timezone.utc),
              sid="clean", conf="high", cluster_size=0),
    ]
    out = apply_cluster_policy(events, ClusterPolicy(action="drop"))
    assert [e.deterministic_id for e in out] == ["clean"]

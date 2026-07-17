from datetime import datetime, timezone

import pytest

from coord_tracker_bridge import CapabilityState, Snapshot, SourceIdentity, WorkRecord


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)


def test_full_source_identity_is_unambiguous_even_with_same_suffix():
    left = SourceIdentity("coord-engine", "fulcra", "alpha-12345678")
    right = SourceIdentity("coord-engine", "fulcra", "beta-12345678")

    assert left.key != right.key
    assert left.to_dict()["item_id"] == "alpha-12345678"


def test_snapshot_rejects_duplicate_full_identity():
    source = SourceIdentity("coord-engine", "fulcra", "task-1")
    item = WorkRecord(source, "tasks", "Task", "active", origin="fleet")

    with pytest.raises(ValueError, match="duplicate source identity"):
        Snapshot((item, item), True, (), {"tasks": CapabilityState.COMPLETE}, NOW)


def test_snapshot_distinguishes_unsupported_from_degraded():
    snapshot = Snapshot(
        (),
        True,
        (),
        {"tasks": CapabilityState.COMPLETE, "asks": CapabilityState.DEGRADED,
         "expectations": CapabilityState.UNSUPPORTED},
        NOW,
    )

    assert snapshot.absence_is_authoritative("tasks")
    assert not snapshot.absence_is_authoritative("asks")
    assert not snapshot.absence_is_authoritative("expectations")


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        Snapshot((), True, (), {}, datetime(2026, 7, 17))

"""Red-first contract tests for the normalized, fail-closed change detector."""

from __future__ import annotations

from coord_engine.budget import Deadline


class FeedTransport:
    """Small real-boundary double: one envelope call and optional record cursor."""

    def __init__(self, envelope, records=None):
        self.envelope = envelope
        self.record_rows = records
        self.feed_calls = 0
        self.record_calls = 0

    def data_updates(self, _since, *, deadline=None):
        self.feed_calls += 1
        return self.envelope

    def records_cursor(self, _channel, _since, *, deadline=None):
        self.record_calls += 1
        return self.record_rows


def _row(path, state="uploaded", at="2026-08-20T12:00:00Z", update_id="u-1"):
    return {"path": path, "state": state, "uploaded_at": at, "update_id": update_id}


def _poll(transport):
    from coord_engine.change_detection import ChangeDetector

    return ChangeDetector(transport).poll(
        "r", "2026-08-20T11:00:00Z", Deadline.open(5.0)
    )


def test_invalid_envelope_is_unknown_before_any_rows_are_consumed():
    """Removing the envelope guard must not turn malformed input into an empty pass."""
    batch = _poll(FeedTransport({"file_changes": "not-a-list"}))
    assert batch.trusted is False
    assert batch.changes == ()
    assert batch.coverage["tasks"].value == "UNKNOWN"


def test_every_supported_namespace_is_explicitly_covered():
    """Dropping a path family from normalization must make its coverage doubtful."""
    transport = FeedTransport({"file_changes": [
        _row("team/r/task/a.md", update_id="task"),
        _row("team/r/directive/a.md", update_id="directive"),
        _row("team/r/review/a/verdicts/a.md", update_id="review"),
        _row("team/r/_coord/forge/feedback/a/x.md", update_id="forge"),
        _row("team/r/presence/a.md", update_id="presence"),
        _row("team/r/roles/reviewer/a.md", update_id="roles"),
        _row("team/r/_coord/acks/a/a.md", update_id="ack"),
        _row("team/r/_coord/summaries.json", update_id="projection"),
    ]})
    batch = _poll(transport)
    assert batch.trusted is True
    assert {name for name, state in batch.coverage.items() if state.value == "DATA"} == {
        "tasks", "directives", "reviews", "forge", "presence_roles",
        "acknowledgments_responses", "projection_metadata",
    }


def test_duplicate_immutable_update_identity_is_consumed_once_and_sorted():
    """Replacing immutable-id dedupe with path dedupe loses legitimate lifecycles."""
    transport = FeedTransport({"file_changes": [
        _row("team/r/task/z.md", at="2026-08-20T12:00:02Z", update_id="z"),
        _row("team/r/task/a.md", at="2026-08-20T12:00:01Z", update_id="a"),
        _row("team/r/task/z.md", at="2026-08-20T12:00:02Z", update_id="z"),
    ]})
    batch = _poll(transport)
    assert [change.update_id for change in batch.changes] == ["a", "z"]
    assert [change.path for change in batch.changes] == ["team/r/task/a.md", "team/r/task/z.md"]


def test_expired_budget_and_unknown_path_make_coverage_unknown():
    """Treating either doubt as clear could publish a watermark past unseen work."""
    from coord_engine.change_detection import ChangeDetector

    expired = ChangeDetector(FeedTransport({"file_changes": []})).poll(
        "r", "2026-08-20T11:00:00Z", Deadline.open(0.0)
    )
    assert expired.trusted is False
    assert expired.coverage["tasks"].value == "UNKNOWN"

    unknown = _poll(FeedTransport({"file_changes": [_row("team/r/future/a.md")] }))
    assert unknown.trusted is False
    assert unknown.coverage["unknown_unsupported"].value == "UNKNOWN"


def test_nonzero_record_count_materializes_real_identities_once_before_dedupe():
    """A count is a trigger, never an identity; a short cursor response is UNKNOWN."""
    transport = FeedTransport(
        {"file_changes": [], "record_counts": {"coordination": 2}},
        records=[
            {"id": "r-2", "recorded_at": "2026-08-20T12:00:02Z"},
            {"id": "r-1", "recorded_at": "2026-08-20T12:00:01Z"},
            {"id": "r-2", "recorded_at": "2026-08-20T12:00:02Z"},
        ],
    )
    batch = _poll(transport)
    assert transport.record_calls == 1
    assert [change.update_id for change in batch.changes] == ["record:r-1", "record:r-2"]
    assert batch.coverage["acknowledgments_responses"].value == "DATA"

    short = _poll(FeedTransport(
        {"file_changes": [], "record_counts": {"coordination": 2}},
        records=[{"id": "r-1", "recorded_at": "2026-08-20T12:00:01Z"}],
    ))
    assert short.trusted is False
    assert short.coverage["acknowledgments_responses"].value == "UNKNOWN"


def test_transport_failure_without_an_envelope_is_unknown():
    """A failed feed call must not be reclassified as a clean empty envelope."""
    batch = _poll(FeedTransport(None))
    assert batch.trusted is False
    assert batch.changes == ()
    assert all(state.value == "UNKNOWN" for state in batch.coverage.values())

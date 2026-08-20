"""Red-first contract tests for the normalized, fail-closed change detector."""

from __future__ import annotations

import json
from pathlib import Path

from coord_engine.budget import Deadline


COORDINATION_TYPE = "MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041"
CHECKPOINT_TYPE = "MomentAnnotation/a09350b2-e245-4348-ae63-bfb35c712c49"
LIVE_ENVELOPE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "live_data_updates_2026-08-20T2013Z.min.json"
)


class FeedTransport:
    """Small real-boundary double: one envelope call and optional record cursor."""

    def __init__(
        self, envelope, records=None, *, data_type=COORDINATION_TYPE,
        supply_default_count=True,
    ):
        if isinstance(envelope, dict) and supply_default_count and "data_types" not in envelope:
            envelope = {"data_types": {data_type: 0}, **envelope}
        self.envelope = envelope
        self.record_rows = records
        self.data_type = data_type
        self.feed_calls = 0
        self.record_calls = 0

    def data_updates(self, _since, *, deadline=None):
        self.feed_calls += 1
        return self.envelope

    def records_cursor(self, _channel, _since, *, deadline=None):
        self.record_calls += 1
        return self.record_rows

    def read_classified(self, _path, *, deadline=None):
        return json.dumps({"data_type": self.data_type}), "ok"


class ConfiguredFeedTransport(FeedTransport):
    """Feed double with the queue authority used to select its record stream."""

    def __init__(self, envelope, records=None, data_type=COORDINATION_TYPE):
        super().__init__(
            envelope, records, data_type=data_type, supply_default_count=False,
        )
        self.record_channels = []

    def records_cursor(self, channel, since, *, deadline=None):
        self.record_channels.append((channel, since))
        return super().records_cursor(channel, since, deadline=deadline)


def _row(path, state="uploaded", at="2026-08-20T12:00:00Z", update_id="u-1"):
    return {"path": path, "state": state, "uploaded_at": at, "update_id": update_id}


def _poll(transport, team="r"):
    from coord_engine.change_detection import ChangeDetector

    return ChangeDetector(transport).poll(
        team, "2026-08-20T11:00:00Z", Deadline.open(5.0)
    )


def _attested_records(count):
    return {
        "after": "2026-08-20T11:00:00Z",
        "through": f"2026-08-20T12:00:{count:02d}Z",
        "records": [
            {"id": f"live-record-{index}", "recorded_at": f"2026-08-20T12:00:{index:02d}Z"}
            for index in range(1, count + 1)
        ],
    }


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


def test_file_without_a_proven_immutable_identity_makes_the_batch_unknown():
    """A path/state/time tuple can collide and must never stand in for identity."""
    row = _row("team/r/task/a.md")
    row.pop("update_id")
    batch = _poll(FeedTransport({"file_changes": [row]}))
    assert batch.trusted is False
    assert batch.coverage["tasks"].value == "UNKNOWN"


def test_lifecycle_timestamps_are_validated_normalized_and_sorted_temporally():
    """Lexical ordering of offset spellings does not order lifecycle instants."""
    transport = FeedTransport({"file_changes": [
        _row("team/r/task/a.md", at="2026-08-20T14:00:00+02:00", update_id="a"),
        _row("team/r/task/b.md", at="2026-08-20T11:30:00Z", update_id="b"),
    ]})
    batch = _poll(transport)
    assert [change.update_id for change in batch.changes] == ["b", "a"]
    assert batch.changes[1].at == "2026-08-20T12:00:00Z"

    fractional = _poll(FeedTransport({"file_changes": [
        _row("team/r/task/c.md", at="2026-08-20T12:00:00.123456+00:00", update_id="c"),
    ]}))
    assert fractional.changes[0].at == "2026-08-20T12:00:00.123456Z"

    malformed = _poll(FeedTransport({"file_changes": [
        _row("team/r/task/a.md", at="not-a-timestamp"),
    ]}))
    assert malformed.trusted is False
    assert malformed.coverage["tasks"].value == "UNKNOWN"


def test_mixed_precision_lifecycle_instants_sort_temporally():
    """A whole-second instant precedes a later fractional-second instant."""
    batch = _poll(FeedTransport({"file_changes": [
        _row(
            "team/r/task/later.md",
            at="2026-08-20T12:00:00.1Z",
            update_id="later",
        ),
        _row(
            "team/r/task/earlier.md",
            at="2026-08-20T12:00:00Z",
            update_id="earlier",
        ),
    ]}))

    assert [change.update_id for change in batch.changes] == ["earlier", "later"]


def test_captured_data_types_materializes_the_configured_coordination_channel_once():
    """Ignoring live ``data_types`` loses real coordination record triggers."""
    envelope = json.loads(LIVE_ENVELOPE_FIXTURE.read_text())
    transport = ConfiguredFeedTransport(envelope, records=_attested_records(13))

    batch = _poll(transport, team="fulcra")

    assert transport.record_calls == 1
    assert transport.record_channels == [(COORDINATION_TYPE, "2026-08-20T11:00:00Z")]
    assert batch.coverage["acknowledgments_responses"].value == "DATA"


def test_host_override_cannot_redirect_detection_from_canonical_bus_authority(
    monkeypatch,
):
    """A writer/test override must not hide work on the stored canonical queue."""
    override_type = "MomentAnnotation/host-local-override"
    monkeypatch.setenv("COORD_RECORDS_TYPE", override_type)
    transport = ConfiguredFeedTransport(
        {
            "data_types": {COORDINATION_TYPE: 1, override_type: 0},
            "file_changes": [],
        },
        records=_attested_records(1),
    )

    batch = _poll(transport)

    assert transport.record_calls == 1
    assert transport.record_channels == [
        (COORDINATION_TYPE, "2026-08-20T11:00:00Z")
    ]
    assert batch.trusted is True
    assert batch.coverage["acknowledgments_responses"].value == "DATA"


def test_authority_lookup_stops_at_detector_deadline_without_retry(monkeypatch):
    """An authority retry after expiry could advance from an over-budget answer."""
    from coord_engine.change_detection import ChangeDetector

    class ManualDeadline:
        def __init__(self):
            self.spent = False

        def expired(self):
            return self.spent

        def remaining(self):
            return 0.0 if self.spent else 5.0

    class ExpiringAuthorityTransport(FeedTransport):
        def __init__(self, deadline):
            super().__init__({"file_changes": []})
            self.deadline = deadline
            self.authority_calls = 0
            self.authority_deadlines = []

        def read_classified(self, _path, *, deadline=None):
            self.authority_calls += 1
            self.authority_deadlines.append(deadline)
            self.deadline.spent = True
            return None, "error"

    monkeypatch.setenv("COORD_READ_RETRY_MS", "1")
    deadline = ManualDeadline()
    transport = ExpiringAuthorityTransport(deadline)

    batch = ChangeDetector(transport).poll(
        "r", "2026-08-20T11:00:00Z", deadline
    )

    assert transport.authority_calls == 1
    assert transport.authority_deadlines == [deadline]
    assert batch.trusted is False
    assert all(state.value == "UNKNOWN" for state in batch.coverage.values())


def test_missing_configured_channel_count_is_unknown_not_clear():
    """A missing live count cannot silently become a zero-count record window."""
    batch = _poll(ConfiguredFeedTransport({
        "data_types": {"AppleLocationUpdate": 50, CHECKPOINT_TYPE: 4},
        "file_changes": [],
    }))

    assert batch.trusted is False
    assert batch.coverage["acknowledgments_responses"].value == "UNKNOWN"


def test_captured_engine_owned_file_shapes_are_explicit_and_trusted():
    """Supported live engine state must not poison feed coverage as unknown."""
    envelope = json.loads(LIVE_ENVELOPE_FIXTURE.read_text())
    batch = _poll(
        ConfiguredFeedTransport(envelope, records=_attested_records(13)),
        team="fulcra",
    )

    assert batch.trusted is True
    assert batch.coverage["unknown_unsupported"].value == "CLEAR"
    assert {change.namespace for change in batch.changes} >= {
        "tasks", "reviews", "presence_roles", "projection_metadata",
        "router_state", "agent_state", "annotation_state", "health", "member_state",
    }


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
        {"file_changes": [], "data_types": {COORDINATION_TYPE: 2}},
        records={"after": "2026-08-20T11:00:00Z", "through": "2026-08-20T12:00:02Z",
                 "records": [
                     {"id": "r-2", "recorded_at": "2026-08-20T12:00:02Z"},
                     {"id": "r-1", "recorded_at": "2026-08-20T12:00:01Z"},
                 ]},
    )
    batch = _poll(transport)
    assert transport.record_calls == 1
    assert [change.update_id for change in batch.changes] == ["record:r-1", "record:r-2"]
    assert batch.coverage["acknowledgments_responses"].value == "DATA"

    short = _poll(FeedTransport(
        {"file_changes": [], "data_types": {COORDINATION_TYPE: 2}},
        records={"after": "2026-08-20T11:00:00Z", "through": "2026-08-20T12:00:02Z",
                 "records": [{"id": "r-1", "recorded_at": "2026-08-20T12:00:01Z"}]},
    ))
    assert short.trusted is False
    assert short.coverage["acknowledgments_responses"].value == "UNKNOWN"


def test_record_cursor_requires_an_attested_exact_boundary_and_supported_channel():
    """A count cannot prove either the cursor window or a future channel's meaning."""
    envelope = {"file_changes": [], "data_types": {COORDINATION_TYPE: 1}}
    no_boundary = _poll(FeedTransport(envelope, records=[
        {"id": "r-1", "recorded_at": "2026-08-20T12:00:01Z"},
    ]))
    assert no_boundary.trusted is False

    outside = _poll(FeedTransport(envelope, records={
        "after": "2026-08-20T11:00:00Z", "through": "2026-08-20T12:00:00Z",
        "records": [{"id": "r-1", "recorded_at": "2026-08-20T12:00:01Z"}],
    }))
    assert outside.trusted is False

    excessive = _poll(FeedTransport(
        {"file_changes": [], "data_types": {COORDINATION_TYPE: 1}},
        records={"after": "2026-08-20T11:00:00Z", "through": "2026-08-20T12:00:02Z",
                 "records": [
                     {"id": "r-1", "recorded_at": "2026-08-20T12:00:01Z"},
                     {"id": "r-2", "recorded_at": "2026-08-20T12:00:02Z"},
                 ]},
    ))
    assert excessive.trusted is False

    unsupported = _poll(ConfiguredFeedTransport({
        "file_changes": [], "data_types": {CHECKPOINT_TYPE: 0},
    }))
    assert unsupported.trusted is False
    assert unsupported.coverage["acknowledgments_responses"].value == "UNKNOWN"


def test_sealed_batch_does_not_expose_mutable_coverage_or_envelope():
    """Frozen dataclasses alone do not stop a caller from rewriting trust facts."""
    batch = _poll(FeedTransport({"file_changes": []}))
    import pytest
    with pytest.raises(TypeError):
        batch.coverage["tasks"] = "UNKNOWN"
    with pytest.raises(TypeError):
        batch.envelope["file_changes"] = []


def test_transport_failure_without_an_envelope_is_unknown():
    """A failed feed call must not be reclassified as a clean empty envelope."""
    batch = _poll(FeedTransport(None))
    assert batch.trusted is False
    assert batch.changes == ()
    assert all(state.value == "UNKNOWN" for state in batch.coverage.values())

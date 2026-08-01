"""Reader-side warnings for legacy control traffic modern queues cannot parse."""
from coord_engine import records


def _rec(note, sender="old-agent"):
    return {
        "id": "r1",
        "recorded_at": "2026-08-01T00:00:00Z",
        "note": note,
        "sources": [sender],
    }


def test_census_flags_prose_notes_from_recognized_senders():
    window = [_rec("create: REVIEW REQUEST: pr-9 · assignee: x")]
    warnings = records.invisible_writer_census(window)
    assert len(warnings) == 1
    assert "old-agent" in warnings[0]
    assert "not parseable" in warnings[0] or "invisible" in warnings[0]


def test_census_ignores_v1_payloads_unattributed_notes_and_free_text():
    good = _rec(records.build_payload(
        to="a", kind="claim", priority="P3", slug="s"))
    unattributed = {
        "id": "r2", "note": "create: something",
        "sources": ["com.fulcradynamics.annotation.x"],
    }
    free_text = _rec("just a human note with no control-plane shape", "human")
    assert records.invisible_writer_census([good, unattributed]) == []
    assert records.invisible_writer_census([free_text]) == []


def test_census_none_window_is_no_evidence():
    assert records.invisible_writer_census(None) == []


def test_census_groups_and_sorts_by_sender():
    warnings = records.invisible_writer_census([
        _rec("update: one", "z-agent"),
        _rec("done: two", "a-agent"),
        _rec("block: three", "a-agent"),
    ])
    assert "2 note(s) from a-agent" in warnings[0]
    assert "1 note(s) from z-agent" in warnings[1]

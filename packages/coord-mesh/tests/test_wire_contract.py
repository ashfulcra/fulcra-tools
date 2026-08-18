"""The field contract, asserted against a REAL transport row.

This file is the answer to the PR 175 failure mode: a suite that stayed green
while every folded row was poisoned, because the fake emitted `record_id` —
the field the code wanted — and the transport emits `id`.

So the fixture here is not hand-written. It is a real
`fulcra-api get-records "MomentAnnotation/<channel>"` row captured on
2026-08-18, with only the payload redacted; its SHAPE is untouched. If the
platform renames a field, these fail.
"""
import json
import os

from coord_mesh import wire

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "real_record.json")


def real_row():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_the_id_field_is_id_not_record_id():
    """THE regression. `record_id` must not appear, and `id` must."""
    row = real_row()
    assert "id" in row
    assert "record_id" not in row, (
        "transport grew a record_id field — wire.F_ID must be re-derived, not guessed"
    )
    assert wire.record_id(row)


def test_every_promised_field_exists_on_a_real_row():
    """wire.REQUIRED_FIELDS is a promise about the real surface, not a wish."""
    assert wire.missing_fields(real_row()) == []


def test_timestamp_field_is_recorded_at():
    row = real_row()
    assert "recorded_at" in row
    for absent in ("timestamp", "ts", "created_at", "at"):
        assert absent not in row, f"transport grew {absent!r}; re-derive F_RECORDED_AT"
    assert wire.recorded_at(row)


def test_note_is_a_string_not_an_object():
    """Callers json.loads() the note. If it ever arrives pre-parsed, every
    envelope read breaks — pin the type against the real row."""
    assert isinstance(real_row()["note"], str)


def test_sources_is_a_list():
    assert isinstance(real_row()["sources"], list)


def test_sender_is_none_when_no_bare_source_exists():
    """A REAL projection row carries only reverse-DNS sources.

    Captured live: authorship is genuinely UNKNOWN for such rows. Returning the
    first element would invent an author named
    'com.fulcradynamics.fulcra-coord.projection.<uuid>'.
    """
    row = real_row()
    assert all("." in s for s in row["sources"]), (
        "fixture no longer exercises the no-bare-source case; recapture one that does"
    )
    assert wire.sender(row) is None


def test_sender_picks_the_bare_entry_when_present():
    row = real_row()
    row["sources"] = list(row["sources"]) + ["coord-opus-worker"]
    assert wire.sender(row) == "coord-opus-worker"


def test_missing_fields_names_what_is_absent():
    assert set(wire.missing_fields({})) == set(wire.REQUIRED_FIELDS)
    assert wire.missing_fields({"id": "x", "recorded_at": "t",
                                "note": "n", "sources": []}) == []


def test_accessors_return_none_rather_than_raising_on_junk():
    """Folds must survive a malformed row; None is the documented answer."""
    for bad in ({}, {"id": ""}, {"id": None}):
        assert wire.record_id(bad) is None
    assert wire.note_text({"note": "   "}) is None
    assert wire.sender({"sources": None}) is None

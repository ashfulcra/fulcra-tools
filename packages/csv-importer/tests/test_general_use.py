"""Pure parser/value contracts and explicit retirement of legacy CSV writes."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from fulcra_csv import ColumnMap, parse_csv
from fulcra_csv.events import DURATION, INSTANT, GenericEvent, coerce_value
from fulcra_csv.fulcra import FulcraClient

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- value coercion ----------


def test_coerce_value_float():
    assert coerce_value("82.4", "float") == 82.4
    assert coerce_value("  82.4  ", "float") == 82.4


def test_coerce_value_int_tolerates_decimal_zero():
    assert coerce_value("180.0", "int") == 180


def test_coerce_value_bool_truthy_strings():
    assert coerce_value("true", "bool") is True
    assert coerce_value("YES", "bool") is True
    assert coerce_value("0", "bool") is False
    assert coerce_value("", "bool") is None


def test_coerce_value_empty_returns_none():
    assert coerce_value("", "float") is None
    assert coerce_value(None, "float") is None  # type: ignore[arg-type]


def test_coerce_value_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown value_type"):
        coerce_value("x", "uuid")


# ---------- instant annotations ----------


def test_parse_instant_has_no_end_time():
    cm = ColumnMap(
        timestamp="timestamp",
        title=None,
        note="note",
        value="mood_score",
        value_type="int",
        tag="tags",
    )
    events = list(parse_csv(FIXTURES / "mood.csv", column_map=cm, annotation_type=INSTANT))
    assert len(events) == 3
    e = events[0]
    assert e.annotation_type == INSTANT
    assert e.end_time is None
    assert e.value == 7
    assert e.note == "morning coffee good"
    assert e.tag == "morning"


def test_duration_event_rejects_missing_end_time():
    """GenericEvent enforces that duration events must have end_time."""
    with pytest.raises(ValueError, match="duration events require end_time"):
        GenericEvent(
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            note="x",
            title=None,
            source_id="id",
            annotation_type=DURATION,
        )


# ---------- value-only rows (no title/note) ----------


def test_value_only_row_synthesizes_note_from_value():
    """A weight CSV has only timestamp + kg — no text column. The parser
    synthesizes a note from str(value) so events still pass GenericEvent
    validation."""
    cm = ColumnMap(timestamp="date", title=None, value="kg", value_type="float")
    events = list(parse_csv(FIXTURES / "weight.csv", column_map=cm, annotation_type=INSTANT))
    assert len(events) == 3
    assert events[0].value == 82.4
    assert events[0].note == "82.4"


# ---------- retired legacy writer boundaries ----------


@pytest.mark.parametrize("method", ["_build_record", "ingest_batch", "run_import"])
@pytest.mark.parametrize("events", [[], None])
def test_legacy_csv_writer_refuses_before_auth_or_input(monkeypatch, method, events):
    """Native equivalents live in test_standalone_v1, using real Click/runtime.

    Old built-in/definition-wrapped writes no longer authorize any record path.
    Even empty input must fail before credentials or source materialization.
    """
    client = FulcraClient()
    monkeypatch.setattr(client, "get_token", lambda: pytest.fail("auth before retirement refusal"))

    class Unreadable:
        def __iter__(self):
            pytest.fail("input before retirement refusal")

    with pytest.raises(RuntimeError, match="Legacy CSV annotation writer retired"):
        getattr(client, method)(
            Unreadable() if events is None else events,
            definition_id="historical-selection",
            data_type="BodyMass",
            dry_run=True,
        )


# ---------- data_fields lift into top-level payload ----------


def test_data_fields_land_in_data_payload():
    cm = ColumnMap(
        timestamp="timestamp",
        title=None,
        note="note",
        value="mood_score",
        value_type="int",
        tag="tags",
        data_fields=(("tags", "primary_tag"),),
    )
    events = list(parse_csv(FIXTURES / "mood.csv", column_map=cm, annotation_type=INSTANT))
    assert events[0].data_fields == {"primary_tag": "morning"}


# ---------- dedup with same-content-different-time ----------


def test_repeat_value_at_different_times_keeps_two_events():
    cm = ColumnMap(timestamp="date", title=None, value="kg")
    events = list(parse_csv(FIXTURES / "weight.csv", column_map=cm, annotation_type=INSTANT))
    ids = [e.source_id for e in events]
    assert len(ids) == len(set(ids))


# ---------- annotation_type validation ----------


def test_parse_csv_rejects_unknown_annotation_type():
    with pytest.raises(ValueError, match="annotation_type must be"):
        list(parse_csv(FIXTURES / "weight.csv", annotation_type="lifetime"))

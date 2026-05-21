"""Tests for non-media use cases: instant annotations, measurement values,
built-in data types."""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
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
    cm = ColumnMap(timestamp="timestamp", title=None, note="note", value="mood_score",
                   value_type="int", tag="tags")
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


# ---------- built-in type targeting ----------

def _mock_transport(captured: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/data/v1alpha1/event/BodyMass":
            return httpx.Response(200, json=[])
        if request.url.path == "/ingest/v1/record/batch":
            captured.append(request.read())
            return httpx.Response(204)
        if request.url.path.startswith("/user/v1alpha1/tag"):
            return httpx.Response(200, json={"id": "tag-id"})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_run_import_to_builtin_type_omits_annotation_def_source(monkeypatch):
    """Targeting a built-in type (no --definition-id) must not append the
    annotation-def source-id, so dedup works against native records."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-token")
    captured: list[bytes] = []
    client = FulcraClient(transport=_mock_transport(captured))
    events = [GenericEvent(
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        note="82.4",
        title=None,
        source_id="prefix.weight-row-1",
        annotation_type=INSTANT,
        value=82.4,
        data_fields={"unit": "kg"},
    )]
    client.run_import(events, data_type="BodyMass")
    assert len(captured) == 1
    line = json.loads(captured[0])
    # source array contains only the row source_id — no
    # com.fulcradynamics.annotation.<uuid> entry
    assert line["metadata"]["source"] == ["prefix.weight-row-1"]
    assert line["metadata"]["data_type"] == "BodyMass"
    payload = json.loads(line["data"])
    assert payload["value"] == 82.4
    assert payload["unit"] == "kg"
    # No note (None text) is fine on built-in types
    assert payload.get("note") == "82.4"
    assert "title" not in payload
    # Instant: only start_time in recorded_at
    assert "end_time" not in line["metadata"]["recorded_at"]


def test_run_import_to_user_def_keeps_annotation_def_source(monkeypatch):
    """When --definition-id is provided, the source array has both entries."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-token")
    captured: list[bytes] = []
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/data/v1alpha1/event/"):
            return httpx.Response(200, json=[])
        if request.url.path == "/ingest/v1/record/batch":
            captured.append(request.read())
            return httpx.Response(204)
        return httpx.Response(200, json={"id": "x"})
    client = FulcraClient(transport=httpx.MockTransport(handler))
    events = [GenericEvent(
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        note="hello",
        title="hello",
        source_id="prefix.row-1",
        annotation_type=DURATION,
    )]
    client.run_import(events, definition_id="abc-123")
    line = json.loads(captured[0])
    assert line["metadata"]["source"] == [
        "prefix.row-1",
        "com.fulcradynamics.annotation.abc-123",
    ]


# ---------- data_fields lift into top-level payload ----------

def test_data_fields_land_in_data_payload():
    cm = ColumnMap(
        timestamp="timestamp", title=None, note="note", value="mood_score",
        value_type="int", tag="tags",
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

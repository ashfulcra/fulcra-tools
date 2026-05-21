"""Export-to-CSV: column resolution + write_csv shape."""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from fulcra_csv.export import (
    DEFAULT_COLUMNS, ExportOptions, select_column, write_csv,
)


def _rec(data: dict | None = None, **top) -> dict:
    """Helper: build a record with `data` as the JSON-encoded string the
    real API returns. Other top-level fields override the record."""
    out: dict = {}
    if data is not None:
        out["data"] = json.dumps(data)
    out.update(top)
    return out


def test_default_columns_resolve_against_a_user_def_record():
    rec = _rec(
        data={"note": "morning weight", "value": 80.4, "unit": "kg"},
        recorded_at={"start_time": "2026-05-18T08:00:00Z", "end_time": "2026-05-18T08:00:00Z"},
        sources=["com.fulcra.media.v1.abc", "com.fulcradynamics.annotation.def-uuid"],
        tag_names=["weight"],
    )
    opts = ExportOptions()
    assert select_column(rec, "start_time", opts) == "2026-05-18T08:00:00Z"
    assert select_column(rec, "end_time",   opts) == "2026-05-18T08:00:00Z"
    assert select_column(rec, "tag",        opts) == "weight"
    assert select_column(rec, "note",       opts) == "morning weight"
    assert select_column(rec, "value",      opts) == "80.4"


def test_source_id_returns_the_non_def_marker():
    rec = _rec(sources=[
        "com.fulcra.media.v1.abc123",
        "com.fulcradynamics.annotation.def-uuid",
    ])
    assert select_column(rec, "source_id", ExportOptions()) == "com.fulcra.media.v1.abc123"


def test_definition_id_strips_the_prefix():
    rec = _rec(sources=[
        "com.fulcra.media.v1.abc",
        "com.fulcradynamics.annotation.6b35c8ed-1234-5678-90ab-cdef00000000",
    ])
    assert select_column(rec, "definition_id", ExportOptions()) == \
        "6b35c8ed-1234-5678-90ab-cdef00000000"


def test_definition_id_empty_when_no_def_source():
    rec = _rec(sources=["com.fulcra.attention.v1.xyz"])
    assert select_column(rec, "definition_id", ExportOptions()) == ""


def test_data_dot_path_pulls_from_data_payload():
    rec = _rec(data={
        "note": "n",
        "external_ids": {"chrome_identity": "ash@fulcra.com"},
        "service": "web",
    })
    opts = ExportOptions()
    assert select_column(rec, "data.service", opts) == "web"
    assert select_column(rec, "external_ids.chrome_identity", opts) == "ash@fulcra.com"


def test_missing_field_returns_empty_string():
    rec = _rec(data={"note": "n"})
    assert select_column(rec, "data.does_not_exist", ExportOptions()) == ""
    assert select_column(rec, "external_ids.nope", ExportOptions()) == ""


def test_tags_joins_multiple_tag_names():
    rec = _rec(tag_names=["attention", "web", "machine:dbp"])
    assert select_column(rec, "tags", ExportOptions()) == "attention,web,machine:dbp"


def test_value_is_stringified_consistently():
    """Numeric, bool, list values shouldn't break CSV output. Bools must
    render lowercase so they round-trip through coerce_value's truthy set."""
    rec = _rec(data={"value": 42, "flag": True, "list": [1, 2, 3]})
    opts = ExportOptions(columns=("data.value", "data.flag", "data.list"))
    out = io.StringIO()
    write_csv([rec], out, opts)
    lines = out.getvalue().strip().splitlines()
    assert lines[0] == "data.value,data.flag,data.list"
    # Bool MUST render lowercase — events.coerce_value's bool branch
    # only matches lowercase ("true"/"false"). Capitalised would break
    # export → re-import round trips.
    assert lines[1] == '42,true,"[1,2,3]"'


def test_bool_false_renders_lowercase():
    rec = _rec(data={"consent": False})
    opts = ExportOptions(columns=("data.consent",))
    out = io.StringIO()
    write_csv([rec], out, opts)
    assert out.getvalue().strip().splitlines()[1] == "false"


def test_formula_injection_is_neutralised_by_default():
    """A note that begins with `=`, `+`, `-`, `@`, `\\t` or `\\r` would
    execute as a formula when opened in Excel/Sheets. We prefix `'` so
    spreadsheets show the literal cell content. The single quote isn't
    part of the data — it's a spreadsheet display hint."""
    rec = _rec(data={"note": "=cmd|'/c calc'!A1"})
    opts = ExportOptions(columns=("data.note",))
    out = io.StringIO()
    write_csv([rec], out, opts)
    body = out.getvalue().strip().splitlines()[1]
    # csv.writer quotes the cell because of the single quote and equals;
    # the important assertion is that an `=` is NOT the first char.
    assert not body.lstrip('"').startswith("=")
    assert body.lstrip('"').startswith("'=")


def test_formula_guard_can_be_disabled():
    rec = _rec(data={"note": "=A1"})
    opts = ExportOptions(columns=("data.note",), guard_formulas=False)
    out = io.StringIO()
    write_csv([rec], out, opts)
    assert out.getvalue().strip().splitlines()[1] == "=A1"


def test_formula_guard_idempotent_on_safe_cells():
    rec = _rec(data={"note": "hello world"})
    opts = ExportOptions(columns=("data.note",))
    out = io.StringIO()
    write_csv([rec], out, opts)
    assert out.getvalue().strip().splitlines()[1] == "hello world"


def test_date_format_epoch():
    rec = _rec(recorded_at={"start_time": "2026-05-18T00:00:00Z"})
    opts = ExportOptions(columns=("start_time",), date_format="epoch")
    out = io.StringIO()
    write_csv([rec], out, opts)
    # epoch seconds for 2026-05-18T00:00:00Z
    expected = int(datetime(2026, 5, 18, tzinfo=timezone.utc).timestamp())
    assert out.getvalue().strip().splitlines()[1] == str(expected)


def test_date_format_local_renders_in_target_tz():
    rec = _rec(recorded_at={"start_time": "2026-05-18T14:00:00Z"})
    opts = ExportOptions(
        columns=("start_time",),
        date_format="local",
        local_tz=ZoneInfo("America/New_York"),
    )
    out = io.StringIO()
    write_csv([rec], out, opts)
    body = out.getvalue().strip().splitlines()[1]
    # EDT in May → UTC-04:00
    assert "10:00:00" in body
    assert "-04:00" in body


def test_date_format_local_requires_tz():
    with pytest.raises(ValueError, match="requires local_tz"):
        ExportOptions(date_format="local")


def test_write_csv_returns_row_count_and_writes_header():
    records = [
        _rec(data={"note": "a", "value": 1}, recorded_at={"start_time": "2026-05-18T00:00:00Z"}),
        _rec(data={"note": "b", "value": 2}, recorded_at={"start_time": "2026-05-18T01:00:00Z"}),
        _rec(data={"note": "c", "value": 3}, recorded_at={"start_time": "2026-05-18T02:00:00Z"}),
    ]
    out = io.StringIO()
    n = write_csv(records, out, ExportOptions())
    assert n == 3
    lines = out.getvalue().strip().splitlines()
    assert lines[0] == ",".join(DEFAULT_COLUMNS)
    assert lines[1].startswith("2026-05-18T00:00:00Z,")
    assert lines[3].startswith("2026-05-18T02:00:00Z,")


def test_data_payload_as_dict_not_just_string():
    """Some Fulcra endpoints return data as a dict already, not a JSON string."""
    rec = {"data": {"note": "raw dict", "value": 7}}
    assert select_column(rec, "note", ExportOptions()) == "raw dict"
    assert select_column(rec, "value", ExportOptions()) == "7"


def test_tz_naive_iso_treated_as_utc():
    """A timestamp without an offset should be assumed UTC so the export
    is at least consistent — never silently shifted to local."""
    rec = _rec(recorded_at={"start_time": "2026-05-18T14:00:00"})
    opts = ExportOptions(columns=("start_time",))
    out = io.StringIO()
    write_csv([rec], out, opts)
    assert "2026-05-18T14:00:00Z" in out.getvalue()

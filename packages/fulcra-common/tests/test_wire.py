"""The Fulcra annotation wire format — fulcra_common.wire."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fulcra_common.wire import (
    DURATION_ANNOTATION,
    MOMENT_ANNOTATION,
    build_record,
    duration_definition_payload,
    encode_batch,
    iso_z,
    moment_definition_payload,
)

UTC = timezone.utc


def test_iso_z_formats_with_trailing_z():
    assert iso_z(datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)) == "2026-05-22T12:00:00Z"


def test_annotation_type_constants():
    assert DURATION_ANNOTATION == "DurationAnnotation"
    assert MOMENT_ANNOTATION == "MomentAnnotation"


def test_build_record_duration_uses_a_start_end_range():
    rec = build_record(
        data_type="DurationAnnotation",
        start_time=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        end_time=datetime(2026, 5, 22, 12, 5, 0, tzinfo=UTC),
        data={"note": "hi"}, source_id="src-1", tags=["tag-a", "tag-b"],
        definition_id="def-1",
    )
    assert rec["specversion"] == 1
    assert rec["data"] == json.dumps({"note": "hi"}, sort_keys=True)
    md = rec["metadata"]
    assert md["data_type"] == "DurationAnnotation"
    assert md["recorded_at"] == {
        "start_time": "2026-05-22T12:00:00Z",
        "end_time": "2026-05-22T12:05:00Z",
    }
    assert md["tags"] == ["tag-a", "tag-b"]
    assert md["source"] == ["src-1", "com.fulcradynamics.annotation.def-1"]
    assert md["content_type"] == "application/json"


def test_build_record_moment_uses_a_bare_scalar_recorded_at():
    # A point-in-time event: recorded_at is a bare ISO string, NOT a
    # {start_time} object — that object matches neither arm of Fulcra's
    # recorded_at union and the record is silently dropped.
    rec = build_record(
        data_type="MomentAnnotation",
        start_time=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        data={}, source_id="s", tags=[],
    )
    assert rec["metadata"]["recorded_at"] == "2026-05-22T12:00:00Z"
    assert rec["metadata"]["data_type"] == "MomentAnnotation"


def test_build_record_without_definition_id_has_only_the_source_id():
    rec = build_record(
        data_type="DurationAnnotation",
        start_time=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        end_time=datetime(2026, 5, 22, 12, 1, 0, tzinfo=UTC),
        data={}, source_id="s", tags=[],
    )
    assert rec["metadata"]["source"] == ["s"]


def test_encode_batch_joins_records_with_newlines():
    a = build_record(data_type="DurationAnnotation",
                     start_time=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
                     end_time=datetime(2026, 5, 22, 12, 1, 0, tzinfo=UTC),
                     data={"x": 1}, source_id="a", tags=[])
    b = build_record(data_type="MomentAnnotation",
                     start_time=datetime(2026, 5, 22, 13, 0, 0, tzinfo=UTC),
                     data={"x": 2}, source_id="b", tags=[])
    body = encode_batch([a, b])
    expected = (json.dumps(a, sort_keys=True).encode() + b"\n"
                + json.dumps(b, sort_keys=True).encode())
    assert body == expected


def test_duration_definition_payload_includes_measurement_spec():
    p = duration_definition_payload(name="Watched", description="things watched",
                                    tags=["t1"])
    assert p == {
        "annotation_type": "duration",
        "name": "Watched",
        "description": "things watched",
        "tags": ["t1"],
        "measurement_spec": {
            "measurement_type": "duration",
            "value_type": "duration",
            "unit": None,
        },
    }


def test_moment_definition_payload_omits_measurement_spec():
    # A moment definition carries no measurement_spec — verified against
    # the live Fulcra API.
    p = moment_definition_payload(name="Journal", description="entries", tags=[])
    assert p == {
        "annotation_type": "moment",
        "name": "Journal",
        "description": "entries",
        "tags": [],
    }
    assert "measurement_spec" not in p

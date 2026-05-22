"""The Fulcra annotation wire format — fulcra_common.wire."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fulcra_common.wire import (
    DURATION_ANNOTATION,
    INSTANT_ANNOTATION,
    build_record,
    default_data_type,
    definition_payload,
    encode_batch,
    iso_z,
)

UTC = timezone.utc


def test_iso_z_formats_with_trailing_z():
    assert iso_z(datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)) == "2026-05-22T12:00:00Z"


def test_default_data_type_maps_kind_to_wire_type():
    assert default_data_type("duration") == DURATION_ANNOTATION == "DurationAnnotation"
    assert default_data_type("instant") == INSTANT_ANNOTATION == "InstantAnnotation"


def test_build_record_duration_full_envelope():
    rec = build_record(
        data_type="DurationAnnotation",
        start_time=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        end_time=datetime(2026, 5, 22, 12, 5, 0, tzinfo=UTC),
        data={"note": "hi"},
        source_id="src-1",
        tags=["tag-a", "tag-b"],
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


def test_build_record_instant_omits_end_time():
    rec = build_record(
        data_type="InstantAnnotation",
        start_time=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        data={},
        source_id="s",
        tags=[],
    )
    assert "end_time" not in rec["metadata"]["recorded_at"]
    assert rec["metadata"]["recorded_at"]["start_time"] == "2026-05-22T12:00:00Z"


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
    b = build_record(data_type="DurationAnnotation",
                     start_time=datetime(2026, 5, 22, 13, 0, 0, tzinfo=UTC),
                     end_time=datetime(2026, 5, 22, 13, 1, 0, tzinfo=UTC),
                     data={"x": 2}, source_id="b", tags=[])
    body = encode_batch([a, b])
    expected = (json.dumps(a, sort_keys=True).encode() + b"\n"
                + json.dumps(b, sort_keys=True).encode())
    assert body == expected


def test_definition_payload_duration_defaults_value_type_to_duration():
    p = definition_payload(name="Watched", description="things watched",
                           annotation_type="duration", tags=["t1"])
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


def test_definition_payload_instant_defaults_value_type_to_none():
    p = definition_payload(name="Journal", description="entries",
                           annotation_type="instant", tags=[])
    assert p["measurement_spec"] == {
        "measurement_type": "instant", "value_type": "none", "unit": None,
    }


def test_definition_payload_explicit_value_type_and_unit():
    p = definition_payload(name="Body Mass", description="kg",
                           annotation_type="instant", tags=[],
                           value_type="float", unit="kg")
    assert p["measurement_spec"] == {
        "measurement_type": "instant", "value_type": "float", "unit": "kg",
    }

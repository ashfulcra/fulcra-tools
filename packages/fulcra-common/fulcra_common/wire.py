"""The Fulcra annotation wire format — the single source of truth.

How an annotation is written for POST /ingest/v1/record/batch: the record
envelope (specversion / data / metadata), the data_type values, the
recorded_at shape, the source array, the JSONL batch encoding, and the
annotation-definition payload. Every importer builds records through this
module, so a Fulcra wire-format change is a one-place change here.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

DURATION_ANNOTATION = "DurationAnnotation"
INSTANT_ANNOTATION = "InstantAnnotation"


def iso_z(dt: datetime) -> str:
    """Format a datetime as ISO-8601 with a trailing 'Z'. The caller
    controls precision — pass a second-truncated datetime if whole-second
    timestamps are wanted."""
    return dt.isoformat().replace("+00:00", "Z")


def default_data_type(annotation_type: str) -> str:
    """Map an annotation kind ("duration" / "instant") to its wire
    data_type. "duration" -> DurationAnnotation, anything else -> Instant."""
    return DURATION_ANNOTATION if annotation_type == "duration" else INSTANT_ANNOTATION


def build_record(*, data_type: str, start_time: datetime, data: dict,
                  source_id: str, tags: Sequence[str],
                  end_time: datetime | None = None,
                  definition_id: str | None = None) -> dict:
    """Build one annotation record for the ingest batch.

    `data` is the inner payload — serialised here with sorted keys.
    `end_time` is omitted from recorded_at for instant annotations.
    `definition_id`, when given, appends the annotation-definition source
    entry; omit it for built-in data types, which dedup on source_id alone.
    """
    recorded_at: dict = {"start_time": iso_z(start_time)}
    if end_time is not None:
        recorded_at["end_time"] = iso_z(end_time)
    source = [source_id]
    if definition_id:
        source.append(f"com.fulcradynamics.annotation.{definition_id}")
    return {
        "specversion": 1,
        "data": json.dumps(data, sort_keys=True),
        "metadata": {
            "data_type": data_type,
            "recorded_at": recorded_at,
            "tags": list(tags),
            "source": source,
            "content_type": "application/json",
        },
    }


def encode_batch(records: Sequence[dict]) -> bytes:
    """Encode records as the JSONL body for POST /ingest/v1/record/batch —
    one sorted-key JSON object per line, newline-joined."""
    return b"\n".join(json.dumps(r, sort_keys=True).encode() for r in records)


def definition_payload(*, name: str, description: str, annotation_type: str,
                        tags: Sequence[str], value_type: str | None = None,
                        unit: str | None = None) -> dict:
    """Build the POST body for creating an annotation definition.

    `annotation_type` is "duration" or "instant". When `value_type` is not
    given it defaults to "duration" for a duration definition (the
    measurement IS the elapsed duration) and "none" for an instant one.
    """
    if value_type is None:
        value_type = "duration" if annotation_type == "duration" else "none"
    return {
        "annotation_type": annotation_type,
        "name": name,
        "description": description,
        "tags": list(tags),
        "measurement_spec": {
            "measurement_type": annotation_type,
            "value_type": value_type,
            "unit": unit,
        },
    }

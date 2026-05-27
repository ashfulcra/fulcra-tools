"""The Fulcra annotation wire format — the single source of truth.

How an annotation is written for POST /ingest/v1/record/batch: the record
envelope (specversion / data / metadata), the recorded_at union, the
source array, the JSONL batch encoding, and the annotation-definition
payloads. Every importer builds records through this module, so a Fulcra
wire-format change is a one-place change here.

Verified against the live Fulcra API:
  - recorded_at is a union — a {start_time, end_time} object for a
    duration event, a bare scalar ISO string for a moment (point-in-time)
    event. A {start_time}-only object matches neither and is dropped.
  - point-in-time annotations are `moment` / `MomentAnnotation`; a moment
    definition carries no measurement_spec (a duration definition does).
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

DURATION_ANNOTATION = "DurationAnnotation"
MOMENT_ANNOTATION = "MomentAnnotation"


def iso_z(dt: datetime) -> str:
    """Format a datetime as ISO-8601 with a trailing 'Z'. The caller
    controls precision — pass a second-truncated datetime if whole-second
    timestamps are wanted."""
    return dt.isoformat().replace("+00:00", "Z")


def build_record(*, data_type: str, start_time: datetime, data: dict,
                  source_id: str, tags: Sequence[str],
                  end_time: datetime | None = None,
                  definition_id: str | None = None,
                  extra_source_ids: Sequence[str] = ()) -> dict:
    """Build one annotation record for POST /ingest/v1/record/batch.

    `recorded_at` is a union: a {start_time, end_time} object when
    `end_time` is given (a duration event), or a bare scalar ISO string
    when it is not (a moment / point-in-time event).

    `data` is the inner payload — serialised here with sorted keys.
    `definition_id`, when given, appends the annotation-definition source
    entry; omit it for built-in data types, which dedup on source_id alone.
    `extra_source_ids` are appended to the source array AFTER source_id
    (and before the definition source). Used for cross-source dedup
    fingerprints — Fulcra dedupes when ANY source-id matches an existing
    record, so two importers that emit the same content fingerprint dedupe
    against each other without losing their per-plugin source_ids.
    Duplicate / empty entries are filtered to keep the array clean.
    """
    recorded_at: str | dict
    if end_time is not None:
        recorded_at = {
            "start_time": iso_z(start_time),
            "end_time": iso_z(end_time),
        }
    else:
        recorded_at = iso_z(start_time)
    source: list[str] = [source_id]
    seen = {source_id}
    for extra in extra_source_ids:
        if not extra or extra in seen:
            continue
        source.append(extra)
        seen.add(extra)
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


def duration_definition_payload(*, name: str, description: str,
                                 tags: Sequence[str],
                                 value_type: str = "duration",
                                 unit: str | None = None) -> dict:
    """POST body for creating a *duration* annotation definition. A
    duration definition carries a measurement_spec."""
    return {
        "annotation_type": "duration",
        "name": name,
        "description": description,
        "tags": list(tags),
        "measurement_spec": {
            "measurement_type": "duration",
            "value_type": value_type,
            "unit": unit,
        },
    }


def moment_definition_payload(*, name: str, description: str,
                              tags: Sequence[str]) -> dict:
    """POST body for creating a *moment* (point-in-time) annotation
    definition. A moment definition carries NO measurement_spec —
    verified against the live Fulcra API."""
    return {
        "annotation_type": "moment",
        "name": name,
        "description": description,
        "tags": list(tags),
    }

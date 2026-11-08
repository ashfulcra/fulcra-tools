"""Opt-in v1 media schema and encoding, shared by live/history converters.

Callers must pair these builders with owned destination resolution, captured
account/consent, durable pending batches and exact readback before progress.
Legacy callers do not switch just by importing this module.
"""
from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy

from jsonschema import Draft202012Validator

from fulcra_common.data_types import DataTypeSpec
from fulcra_common.v1 import build_event_record, _utc

from .importers.base import NormalizedEvent, _mark_if_low_confidence

_CATEGORIES = {"watched": "Watched", "listened": "Listened", "read": "Read", "activity": "Activity"}
_MEDIA_FIELDS = {
    # Nullable enrichment permits an honest unknown for old typed annotations
    # whose writer discarded these fields. Migration must not invent values.
    "note": {"type": ["string", "null"]},
    "title": {"type": ["string", "null"]},
    "service": {"type": ["string", "null"]},
    "importer": {"type": ["string", "null"]},
    "category": {"enum": [*_CATEGORIES]},
    "timestamp_confidence": {"enum": ["high", "medium", "low", None]},
    "external_ids": {"type": ["object", "null"]},
    "duration_seconds": {"type": "number", "minimum": 0},
}
_LEGACY_RECORD = {"type": "object", "additionalProperties": False,
    "required": ["id", "data_type", "api_version", "source_id", "source_name", "metadata"],
    "properties": {"id": {"type": "string"}, "data_type": {"type": "string"},
        "api_version": {"const": "v1alpha1"}, "source_id": {"type": "string"},
        "source_name": {"type": ["string", "null"]}, "metadata": {"type": "object"}}}
_MEDIA_VALIDATOR = Draft202012Validator({"type": "object", "properties": deepcopy(_MEDIA_FIELDS)})


def media_spec(category: str) -> DataTypeSpec:
    """Versioned semantic destination; separate from legacy definition IDs."""
    if category not in _CATEGORIES:
        raise ValueError("unsupported media category")
    return DataTypeSpec(key=f"collect.media.{category}.v1", base_type="Event",
                        name=_CATEGORIES[category], description=f"Media {category} history from Collect.",
                        fields=_MEDIA_FIELDS)


def media_history_spec(category: str) -> DataTypeSpec:
    """Explicit audit-capable destination; leave the default v1 spec frozen.

    A live importer can use an explicitly pinned compatible richer destination;
    its served-schema enrichment merge retains this optional provenance.
    """
    base = media_spec(category)
    return DataTypeSpec(key=f"collect.media.{category}.history.v1", base_type="Event",
        name=base.name + ' History', description=base.description,
        fields={**deepcopy(_MEDIA_FIELDS), "legacy_record": deepcopy(_LEGACY_RECORD)})


def build_media_record(event: NormalizedEvent, destination: str, *, tags: Sequence[str] = ()) -> dict:
    """Use the primary importer identity, never a cross-source fingerprint.

    The old typed annotation path discarded enrichment. V1 declares it in the
    custom schema and retains it. Replays have distinct primary IDs even when
    they share a content fingerprint; fingerprints remain provenance/policy
    inputs rather than alternate record UUIDs.
    """
    start, end = _utc(event.start_time), _utc(event.end_time)
    record = build_event_record(
        data_type=destination, source_id=event.deterministic_id,
        start_time=start, end_time=end,
        extra_sources=event.extra_source_ids, tags=tags,
        fields={"note": event.note,
                "title": event.title, "service": event.service, "importer": event.importer,
                "category": event.category, "timestamp_confidence": event.timestamp_confidence,
                "external_ids": event.external_ids,
                "duration_seconds": (end - start).total_seconds()},
    )
    if next(_MEDIA_VALIDATOR.iter_errors(record), None) is not None:
        raise ValueError("media record violates the declared v1 schema")
    if record["note"] is not None:
        record["note"] = _mark_if_low_confidence(record["note"], event.timestamp_confidence, event.external_ids)
    return record

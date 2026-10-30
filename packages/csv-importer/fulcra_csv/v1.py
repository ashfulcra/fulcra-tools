"""CSV v1 family codec and source-authorized enrichment overlay.

Pure conversion: no account, profile, network or media-family dependency.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fulcra_common.data_types import DataTypeSpec
from fulcra_common.json_values import snapshot_json
from fulcra_common.v1 import build_event_record, build_metric_record, record_id_for

DESCRIPTION = "Timestamped CSV observations with preserved source fields."
FIELDS = {
    "csv_annotation_type": {"type": "string", "enum": ["duration", "instant"]},
    "note": {"type": "string"},
    "title": {"type": ["string", "null"]},
    "csv_tag": {"type": ["string", "null"]},
    "csv_extra_tags": {"type": "array", "items": {"type": "string"}},
    "csv_data_fields": {"type": "object", "additionalProperties": True},
    "external_ids": {"type": "object", "additionalProperties": True},
}


def csv_spec(
    base_type,
    *,
    unit=None,
    aggregation="discrete",
    name=None,
    description=DESCRIPTION,
    bootstrap=False,
):
    name = (
        name if name is not None else ("CSV Events" if base_type == "Event" else "CSV Measurements")
    )
    fields = snapshot_json(FIELDS)
    if base_type == "Event":
        fields["csv_value"] = {"type": ["string", "number", "boolean", "null"]}
    key = "csv.event.v1" if base_type == "Event" else "csv.metric.v1"
    if bootstrap:
        encoded = json.dumps(
            snapshot_json({"name": name}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        key = "csv.bootstrap.v1." + hashlib.sha256(encoded).hexdigest()
    return DataTypeSpec(
        key,
        base_type,
        name,
        description,
        fields=fields,
        required_fields=tuple(fields),
        unit=unit if base_type == "Metric" else None,
        aggregation=aggregation if base_type == "Metric" else None,
    )


def build_csv_record(event, destination, *, unit=None, tags=()):
    fields = dict(
        csv_annotation_type=event.annotation_type,
        note=event.note,
        title=event.title,
        csv_tag=event.tag,
        csv_extra_tags=list(event.extra_tags),
        csv_data_fields=snapshot_json(event.data_fields),
        external_ids=snapshot_json(event.external_ids),
    )
    kwargs = dict(
        data_type=destination,
        source_id=event.source_id,
        start_time=event.start_time,
        end_time=event.end_time if event.annotation_type == "duration" else None,
        tags=tags,
        fields=fields,
    )
    if destination.startswith("Metric/"):
        effective = event.data_fields.get("unit")
        if effective != unit or (effective is not None and not isinstance(effective, str)):
            raise ValueError("CSV row unit does not match explicit Metric destination unit")
        return build_metric_record(**kwargs, value=event.value)
    if event.value is not None and not isinstance(event.value, (str, bool, int, float)):
        raise ValueError("CSV Event value must be a JSON scalar or null")
    fields["csv_value"] = event.value
    return build_event_record(**kwargs)


def primary_source(record):
    sources = record.get("sources")
    if (
        not isinstance(sources, list)
        or not sources
        or any(not isinstance(s, str) or not s.strip() for s in sources)
    ):
        raise ValueError("CSV existing record primary source is malformed")
    return sources[0]


def _overlay(prior, incoming):
    result = snapshot_json(prior)
    for key, value in incoming.items():
        result[key] = (
            _overlay(result[key], value)
            if isinstance(result.get(key), dict) and isinstance(value, dict)
            else snapshot_json(value)
        )
    return result


def merge_csv_record(current, incoming, declared_schema):
    """Retain declared compatible enrichment; incoming null is an explicit value.

    Caller validates the returned whole record against the bound served schema.
    Server metadata and undeclared fields are never copied into a new write.
    """
    if current is None:
        return snapshot_json(incoming)
    if primary_source(current) != primary_source(incoming):
        raise ValueError(
            "CSV existing UUID has a different primary source; reconcile before import"
        )
    if current.get("id") != incoming["id"]:
        raise ValueError("CSV existing UUID does not match incoming UUID")
    tags = current.get("tags")
    if not isinstance(tags, list):
        raise ValueError("CSV existing tags are malformed")
    for tag in tags:
        try:
            if not isinstance(tag, str) or str(UUID(tag)) != tag:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("CSV existing tags are malformed") from None
    # Prior declared fields are checked independently before overlay so a bad
    # current value cannot be silently hidden by a valid incoming replacement.
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from fulcra_api.record_validation import record_format_checker

    prior = snapshot_json(
        {key: value for key, value in current.items() if key in declared_schema["properties"]}
    )
    validator = Draft202012Validator(
        declared_schema, format_checker=record_format_checker("v1"), registry=Registry()
    )
    if not validator.is_valid(prior):
        raise ValueError("CSV existing declared enrichment is incompatible with served schema")
    result = {**prior, **snapshot_json(incoming)}
    for key in ("external_ids", "csv_data_fields"):
        if not isinstance(prior.get(key), dict) or not isinstance(incoming[key], dict):
            raise ValueError("CSV existing enrichment maps are malformed")
        result[key] = _overlay(prior[key], incoming[key])
    result["tags"] = list(dict.fromkeys(tags + incoming["tags"]))
    result["sources"] = list(dict.fromkeys(incoming["sources"] + current["sources"]))
    return snapshot_json(result)


__all__ = ["csv_spec", "build_csv_record", "primary_source", "record_id_for", "merge_csv_record"]

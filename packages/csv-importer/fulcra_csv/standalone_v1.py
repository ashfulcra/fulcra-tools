"""Standalone CSV authority, full preflight and frozen durable delivery batches."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from uuid import NAMESPACE_URL, uuid5

from fulcra_common.client import ImportResult
from fulcra_common.data_types import tag_create_name
from fulcra_common.v1 import V1DataClient, record_matches
from fulcra_collect.standalone_v1 import standalone_runtime
from fulcra_collect.v1_outbox import V1RecordOutbox

from .v1 import build_csv_record, csv_spec, merge_csv_record

PENDING = "v1_csv_pending"


@dataclass(frozen=True)
class Progress:
    total: int = 0
    skipped_existing: int = 0
    posted: int = 0
    verified: int = 0


class CSVWriter:
    def __init__(self, ctx, session, destination, spec, unit):
        self.ctx, self.session = ctx, session
        self.destination, self.spec, self.unit = destination, spec, unit
        self.outbox = V1RecordOutbox(ctx, session, key=PENDING)
        self.progress = Progress()
        self.recovered = 0

    def resume(self):
        try:
            self.outbox.drain()
        finally:
            progress = self.outbox.progress
            self.recovered = progress.verified if progress else 0

    def run_import(self, events, *, chunk_size=500):
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("CSV chunk size must be a positive integer")
        events = list(events)
        self.progress = Progress(total=len(events))
        with self.session.record_write_scope():
            bound = self.session.resolve_destination(
                self.spec, pinned_id=self.destination, allow_create=False
            )
            rows, selected = {}, {}
            for number, event in enumerate(events, 2):
                try:
                    row = build_csv_record(event, self.destination, unit=self.unit)
                    for label in ([event.tag] if event.tag else []) + list(event.extra_tags):
                        tag_create_name(label)
                    if row["id"] in rows:
                        if not (
                            record_matches(rows[row["id"]], row)
                            and record_matches(row, rows[row["id"]])
                        ):
                            raise ValueError("conflicting input rows for one CSV identity")
                        self.progress = replace(
                            self.progress, skipped_existing=self.progress.skipped_existing + 1
                        )
                    else:
                        rows[row["id"]], selected[row["id"]] = row, event
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"CSV row {number}: {exc}") from None
            api = V1DataClient(self.session.client)
            api.validate_records(self.destination, list(rows.values()))
            # Finish ALL primary-source and prior-enrichment proofs before tag
            # creation or any current record mutation, including later chunks.
            existing = self.session.get_records(self.destination, list(rows)) if rows else {}
            merged = {
                identity: merge_csv_record(existing.get(identity), row, bound.schema)
                for identity, row in rows.items()
            }
            api.validate_records(self.destination, list(merged.values()))
            # Read-only name resolution is part of planning, not metadata setup.
            # Missing IDs can be created only for the narrow ordinary UUID-list
            # schema below; arbitrary UUID-dependent constraints need a real ID.
            tag_ids, planned, known_skips = {}, {}, set()
            for identity, event in selected.items():
                labels = ([event.tag] if event.tag else []) + list(event.extra_tags)
                for label in labels:
                    if label not in tag_ids:
                        try:
                            tag_ids[label] = self.session.resolve_tag(label, allow_create=False)
                        except ValueError as exc:
                            if str(exc) != "no existing tag configured for read-only verification":
                                raise
                            tag_ids[label] = None
                incoming = build_csv_record(
                    event,
                    self.destination,
                    unit=self.unit,
                    tags=[
                        tag_ids[label] or str(uuid5(NAMESPACE_URL, "csv:planned-tag:" + label))
                        for label in labels
                    ],
                )
                planned[identity] = merge_csv_record(existing.get(identity), incoming, bound.schema)
                if all(tag_ids[label] is not None for label in labels) and record_matches(
                    existing.get(identity), planned[identity]
                ):
                    known_skips.add(identity)
                    self.progress = replace(
                        self.progress, skipped_existing=self.progress.skipped_existing + 1
                    )
            if any(identity is None for identity in tag_ids.values()) and not _ordinary_tag_schema(
                bound.schema
            ):
                raise ValueError(
                    "missing CSV tags require an unconstrained UUID-list schema; configure compatible existing tags before import"
                )
            api.validate_records(self.destination, list(planned.values()))
            for label, identity in tag_ids.items():
                if identity is not None:
                    self.session.remember_existing_tag(label, identity)
            # No exclusion repair or tag creation precedes the finalized union
            # proof. Planned UUIDs are never staged or submitted.
            excluded = self.session.excluded_ids(self.destination, list(rows)) if rows else set()
            self.progress = replace(
                self.progress,
                skipped_existing=self.progress.skipped_existing + len(excluded - known_skips),
            )
            candidates = []
            finalized = []
            for identity, event in selected.items():
                if identity in excluded:
                    continue
                labels = ([event.tag] if event.tag else []) + list(event.extra_tags)
                for label in labels:
                    if tag_ids[label] is None:
                        tag_ids[label] = self.session.resolve_tag(label)
                incoming = build_csv_record(
                    event,
                    self.destination,
                    unit=self.unit,
                    tags=[tag_ids[label] for label in labels],
                )
                row = merge_csv_record(existing.get(identity), incoming, bound.schema)
                finalized.append(row)
                if identity not in known_skips:
                    if record_matches(existing.get(identity), row):
                        self.progress = replace(
                            self.progress, skipped_existing=self.progress.skipped_existing + 1
                        )
                    else:
                        candidates.append(row)
            api.validate_records(self.destination, finalized)
            for offset in range(0, len(candidates), chunk_size):
                self.outbox.stage(
                    {self.destination: candidates[offset : offset + chunk_size]},
                    message="Imported CSV records",
                )
                baseline = self.progress
                try:
                    self.outbox.drain()
                finally:
                    p = self.outbox.progress
                    if p is not None:
                        self.progress = replace(
                            baseline,
                            posted=baseline.posted + p.verified,
                            verified=baseline.verified + p.verified,
                            skipped_existing=baseline.skipped_existing + p.excluded,
                        )
        return ImportResult(**vars(self.progress))


def _ordinary_tag_schema(schema):
    """Conservative creation proof for unknown UUIDs, not a schema interpreter.

    A plain object with independent properties and ordinary UUID-list tags
    accepts every possible canonical created tag ID and deduplicated union.
    Count/enum/combinator/conditional constraints require real existing IDs;
    read-only resolution and whole-schema validation still support those IDs.
    """
    root_keys = {
        "$schema",
        "$id",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "$defs",
        "definitions",
    }
    if set(schema) - root_keys or schema.get("type") != "object":
        return False
    tags = schema["properties"].get("tags")
    if (
        not isinstance(tags, dict)
        or set(tags) - {"type", "items", "uniqueItems"}
        or tags.get("type", "array") != "array"
    ):
        return False
    items = tags.get("items", {})
    return (
        isinstance(items, dict)
        and not set(items) - {"type", "format"}
        and items.get("type", "string") == "string"
        and items.get("format", "uuid") == "uuid"
    )


def authority(destination, *, unit=None, aggregation="discrete", legacy_selection=None):
    spec = csv_spec(destination.split("/")[0], unit=unit, aggregation=aggregation)
    return dict(
        destination=destination,
        spec=spec.fingerprint,
        unit=unit,
        aggregation=aggregation if destination.startswith("Metric/") else None,
        legacy_selection=legacy_selection,
        codec="csv.v1",
    )


@contextmanager
def open_csv(settings, *, client, on_writer=None):
    transport = client._transport
    with standalone_runtime(
        "cli-csv", settings, client=client, transport=transport, identity_transport=transport
    ) as (ctx, session):
        destination = settings["destination"]
        writer = CSVWriter(
            ctx,
            session,
            destination,
            csv_spec(
                destination.split("/")[0],
                unit=settings["unit"],
                aggregation=settings["aggregation"],
            ),
            settings["unit"],
        )
        if on_writer is not None:
            on_writer(writer)
        # Recovery must precede opening even missing or newly empty sources.
        writer.resume()
        yield writer

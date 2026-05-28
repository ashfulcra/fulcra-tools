"""Unified ingest pipeline — every Fulcra annotation POST goes through here.

Owns the single `wire.build_record` callsite, the single `data_inner` shape
including the #30 `duration_seconds` defensive field, the single source-array
construction, and the single POST + retry policy. Importers build typed
`IngestableEvent`s (one of `MomentEvent` / `DurationEvent`) and hand them to
`IngestPipeline.ingest_batch`; the pipeline does the rest.

Design note on the dedicated-field shape:
    The legacy ad-hoc sites each emitted a slightly different `data_inner`
    shape. Some fields (note, title, service, timestamp_confidence,
    external_ids, duration_seconds) are common across importers and live as
    dedicated fields on `IngestableEvent`. Importer-specific top-level
    fields (attention's category/url/og_description/favicon_url/
    parent_source_id; daemon's quick-record comment; tombstone's
    superseded_by/supersedes_source_id) are added as additional optional
    dedicated fields on `IngestableEvent` rather than being routed through
    `external_ids`. This was the explicit choice in refactor #69: byte
    parity is the whole point, so the wire shape is preserved verbatim.
    A regression test in tests/test_ingest_byte_parity.py guarded the
    cutover; it is deleted in Phase 3 once all callsites use the pipeline.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from fulcra_common import wire
from fulcra_common.client import BaseFulcraClient


@dataclass
class IngestableEvent:
    """Base for every annotation event the pipeline can ingest.

    Subclasses add the time-shape: `MomentEvent` carries a `ts`,
    `DurationEvent` carries `start` + `end`. Everything else is shared.

    `extra_source_ids` is precomputed per-importer (e.g. the
    cross-source listened/watched fingerprint) — the pipeline does NOT
    centralize that computation, see scoping doc Risk #3.

    `definition_id` is `str | None` because the daemon tombstone path
    writes an event with no annotation definition attached (see
    fulcra_collect/daemon.py:_delete_annotation). For every other
    importer it is required.
    """
    definition_id: str | None
    source_id: str
    extra_source_ids: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    external_ids: dict = field(default_factory=dict)
    note: str | None = None
    title: str | None = None
    service: str | None = None
    timestamp_confidence: str | None = None
    # Quick-record-only field. Distinct from `note` — every other importer
    # uses note/title, but the menubar's _record_annotation emits
    # `data.comment` directly and the tombstone in _delete_annotation
    # follows the same shape. Keeping `comment` as a dedicated top-level
    # data field preserves byte parity with the legacy daemon site.
    comment: str | None = None
    # Attention extension fields — these were top-level keys in the legacy
    # attention `data_inner`. Kept dedicated rather than nested under
    # `external_ids` so the wire payload stays byte-identical to today's
    # output. None is a valid wire value here (e.g. category=None when url
    # is set; parent_source_id is always None pending v2 highlights), so
    # the build_record path emits them whenever the importer opts in via
    # `_emit_attention_fields=True`.
    category: str | None = None
    url: str | None = None
    og_description: str | None = None
    favicon_url: str | None = None
    parent_source_id: str | None = None
    # Tombstone-only fields. The daemon's _delete_annotation writes
    # `data.superseded_by` and `data.supersedes_source_id` at top-level;
    # dedicated fields preserve that shape.
    superseded_by: str | None = None
    supersedes_source_id: str | None = None
    # Flag: when True, the pipeline emits the five attention extension
    # fields (category/url/og_description/favicon_url/parent_source_id)
    # at top-level data even when their values are None. Attention's
    # legacy shape always emits these keys (with None values when not
    # applicable); other importers must NOT see them at all. Set by
    # build_attention_event.
    _emit_attention_fields: bool = False


@dataclass
class MomentEvent(IngestableEvent):
    """Point-in-time annotation. Wire `recorded_at` is a bare ISO string."""
    ts: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.ts is None:
            raise ValueError("MomentEvent.ts is required")
        if self.ts.tzinfo is None:
            raise ValueError("MomentEvent.ts must be timezone-aware")


@dataclass
class DurationEvent(IngestableEvent):
    """Duration annotation. Wire `recorded_at` is {start_time, end_time}."""
    start: datetime = None  # type: ignore[assignment]
    end: datetime = None    # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.start is None or self.end is None:
            raise ValueError("DurationEvent requires start and end")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("DurationEvent.start/end must be timezone-aware")

    @property
    def duration_seconds(self) -> int:
        # Clamp to zero — see legacy sites in fulcra_attention/ingest.py
        # and fulcra_media/fulcra.py for the #30 rationale. A misordered
        # start/end shouldn't propagate a negative into the wire payload.
        return max(0, int((self.end - self.start).total_seconds()))


class IngestPipeline:
    """The single ingest path. Wraps a `BaseFulcraClient` (for the POST)
    and owns the wire-format construction for every `IngestableEvent`."""

    def __init__(self, client: BaseFulcraClient | None) -> None:
        # `client` is optional only because `build_record` is pure.
        # `ingest_batch` will require it.
        self.client = client

    def build_record(self, event: IngestableEvent) -> dict:
        """Build a single wire-format dict from an `IngestableEvent`.

        Pure — no I/O. The single place `duration_seconds` is added to a
        Duration payload (the #30 defensive field that used to live in 3
        ad-hoc sites).
        """
        data_inner: dict = {}
        # Common optional fields — only emitted when set, matching the
        # csv-importer's conservative approach (avoids polluting built-in
        # Fulcra schemas with empty keys). Media always sets these so
        # this is a no-op for them. Attention's legacy site emitted
        # `note` and `title` at top-level data UNCONDITIONALLY (even when
        # title is None for the category-variant payload); the
        # `_emit_attention_fields` opt-in below also forces note + title
        # to be emitted to preserve byte parity.
        if event.note is not None or event._emit_attention_fields:
            data_inner["note"] = event.note
        if event.title is not None or event._emit_attention_fields:
            data_inner["title"] = event.title
        if event.service is not None:
            data_inner["service"] = event.service
        if event.timestamp_confidence is not None:
            data_inner["timestamp_confidence"] = event.timestamp_confidence
        if event.comment is not None:
            data_inner["comment"] = event.comment
        # Attention's legacy site emits these five keys at top-level data
        # unconditionally (with None values when not applicable). Honor
        # that shape only when the importer explicitly opts in.
        if event._emit_attention_fields:
            data_inner["category"] = event.category
            data_inner["url"] = event.url
            data_inner["og_description"] = event.og_description
            data_inner["favicon_url"] = event.favicon_url
            data_inner["parent_source_id"] = event.parent_source_id
        if event.superseded_by is not None:
            data_inner["superseded_by"] = event.superseded_by
        if event.supersedes_source_id is not None:
            data_inner["supersedes_source_id"] = event.supersedes_source_id
        if event.external_ids:
            data_inner["external_ids"] = event.external_ids

        if isinstance(event, DurationEvent):
            # The #30 defensive field — injected here, ONCE.
            data_inner["duration_seconds"] = event.duration_seconds
            data_type = wire.DURATION_ANNOTATION
            start_time = event.start
            end_time: datetime | None = event.end
        elif isinstance(event, MomentEvent):
            data_type = wire.MOMENT_ANNOTATION
            start_time = event.ts
            end_time = None
        else:
            raise TypeError(
                f"unknown IngestableEvent subclass: {type(event).__name__}"
            )

        return wire.build_record(
            data_type=data_type,
            start_time=start_time,
            end_time=end_time,
            data=data_inner,
            source_id=event.source_id,
            tags=list(event.tags),
            definition_id=event.definition_id,
            extra_source_ids=event.extra_source_ids,
        )

    def ingest_one(self, event: IngestableEvent) -> None:
        """Post a single event. Convenience around `ingest_batch`."""
        self.ingest_batch([event])

    def ingest_batch(self, events: Iterable[IngestableEvent]) -> None:
        """Post a batch of events to /ingest/v1/record/batch.

        No-ops on empty input. Caller is responsible for the dedup
        readback (per-importer concern, lives on the subclass's
        `run_import`). Raises httpx.HTTPStatusError on non-2xx — caller
        decides retry policy.
        """
        if self.client is None:
            raise RuntimeError(
                "IngestPipeline.ingest_batch needs a BaseFulcraClient; "
                "pipeline was built with client=None (build_record-only).",
            )
        events = list(events)
        if not events:
            return
        body = wire.encode_batch([self.build_record(e) for e in events])
        r = self.client._client().post(
            "/ingest/v1/record/batch",
            content=body,
            headers={
                **self.client._authed_headers(),
                "content-type": "application/x-jsonl",
            },
        )
        r.raise_for_status()

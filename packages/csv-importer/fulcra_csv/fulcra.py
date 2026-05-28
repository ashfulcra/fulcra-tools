"""Lightweight Fulcra ingest client for general CSV → annotation.

Builds on `fulcra_common.BaseFulcraClient`. Service-agnostic: the caller
supplies the target annotation definition id and optional tag ids.
Supports both DurationAnnotation and InstantAnnotation shapes; data_type
can be overridden for custom annotation kinds.

`ImportResult` is re-exported here so existing
`from fulcra_csv.fulcra import ImportResult` imports keep working.
"""
from __future__ import annotations

from datetime import timedelta

from fulcra_common import BaseFulcraClient, ImportResult
from fulcra_common import wire

from .events import DURATION, GenericEvent

__all__ = ["FulcraClient", "ImportResult"]


def _default_data_type(annotation_type: str) -> str:
    return "DurationAnnotation" if annotation_type == DURATION else "InstantAnnotation"


class FulcraClient(BaseFulcraClient):
    USER_AGENT = "fulcra-csv-importer/0.1"
    # follow_redirects=False so the per-request Authorization header never
    # rides along on a 3xx to a host the user didn't intend. This client
    # percent-encodes tag names (quote_name below), so it doesn't depend on
    # following the 303 the tag-name lookup answers for some names.
    FOLLOW_REDIRECTS = False

    def ensure_tag(self, name: str) -> str:
        # quote_name=True percent-encodes the name in the lookup path so
        # tags with `/`, `?`, `#`, or spaces don't break the GET.
        return self._resolve_tag(name, quote_name=True)

    def _build_record(
        self,
        ev: GenericEvent,
        *,
        definition_id: str | None,
        tag_id_for: dict[str, str],
        data_type: str | None,
    ) -> dict:
        # Resolve the single `tag` plus any `extra_tags` to tag ids, in
        # order, de-duplicated.
        tag_ids: list[str] = []
        for name in ([ev.tag] if ev.tag else []) + list(ev.extra_tags):
            tid = tag_id_for.get(name)
            if tid and tid not in tag_ids:
                tag_ids.append(tid)

        if (
            ev.annotation_type == DURATION
            and ev.end_time is not None
            and (data_type is None or data_type == "DurationAnnotation")
        ):
            # Route duration CSV rows through IngestPipeline (refactor
            # #69) so they share the wire-format construction with every
            # other importer. GenericEvent carries free-form
            # `data_fields`, a `value` echo, and a `tag` echo that the
            # legacy site emits at TOP-LEVEL `data.*`. The pipeline does
            # not model those importer-specific axes, so we merge them
            # into the built record's data payload as a post-build step
            # — keeps the pipeline clean of csv-importer-specific keys
            # while preserving the wire shape.
            from fulcra_common.ingest import DurationEvent, IngestPipeline
            external = dict(ev.external_ids) if ev.external_ids else {}
            duration_event = DurationEvent(
                definition_id=definition_id,
                source_id=ev.source_id,
                tags=tuple(tag_ids),
                external_ids=external,
                note=ev.note or None,
                title=ev.title or None,
                start=ev.start_time,
                end=ev.end_time,
            )
            rec = IngestPipeline(client=None).build_record(duration_event)
            # GenericEvent's `value`, `tag` echo, and free-form
            # `data_fields` are CSV-importer-specific top-level data
            # keys. Merge into the built record's data payload so the
            # wire shape matches today's output byte-for-byte.
            import json
            payload = json.loads(rec["data"])
            if ev.value is not None and "value" not in payload:
                payload["value"] = ev.value
            if ev.tag and "tag" not in payload:
                payload["tag"] = ev.tag
            for k, v in ev.data_fields.items():
                payload.setdefault(k, v)
            # Minor behaviour change vs the legacy site: the legacy site
            # only emitted `duration_seconds` when duration > 0; the
            # pipeline always emits it (including 0) for any DurationEvent.
            # `data.duration_seconds=0` is observably the same value to
            # every Fulcra consumer, so we accept this tiny shape change.
            # Re-serialize with sorted keys so the wire bytes stay
            # canonical (matches wire.build_record's json.dumps shape).
            rec["data"] = json.dumps(payload, sort_keys=True)
            return rec

        # Instant events stay on the legacy path. The pipeline only
        # models Moment / Duration; csv-importer's `InstantAnnotation`
        # is a distinct data_type on the wire, and routing it through
        # MomentEvent would silently change the data_type string
        # (InstantAnnotation → MomentAnnotation). Keep the legacy build
        # for instants — deliberate, not laziness. See refactor #69
        # cutover #3 commit for the discussion.
        data_inner: dict = {}
        if ev.note:
            data_inner["note"] = ev.note
        if ev.title:
            data_inner["title"] = ev.title
        if ev.value is not None:
            data_inner["value"] = ev.value
        if ev.tag:
            data_inner["tag"] = ev.tag
        data_inner.update(ev.data_fields)
        if ev.external_ids:
            data_inner["external_ids"] = ev.external_ids

        return wire.build_record(
            data_type=data_type or _default_data_type(ev.annotation_type),
            start_time=ev.start_time,
            end_time=None,
            data=data_inner,
            source_id=ev.source_id,
            tags=tag_ids,
            definition_id=definition_id,
        )

    def ingest_batch(
        self,
        events: list[GenericEvent],
        *,
        definition_id: str | None = None,
        tag_id_for: dict[str, str] | None = None,
        data_type: str | None = None,
    ) -> None:
        if not events:
            return
        tag_id_for = tag_id_for or {}
        body = wire.encode_batch([
            self._build_record(
                ev, definition_id=definition_id, tag_id_for=tag_id_for,
                data_type=data_type,
            )
            for ev in events
        ])
        r = self._client().post(
            "/ingest/v1/record/batch",
            content=body,
            headers={
                **self._authed_headers(),
                "content-type": "application/x-jsonl",
            },
        )
        r.raise_for_status()

    def run_import(
        self,
        events: list[GenericEvent],
        *,
        definition_id: str | None = None,
        tag_id_for: dict[str, str] | None = None,
        data_type: str | None = None,
        chunk_size: int = 500,
        window_pad_minutes: int = 10,
    ) -> ImportResult:
        events = list(events)
        total = len(events)
        if total == 0:
            return ImportResult(0, 0, 0, 0)

        events_sorted = sorted(events, key=lambda e: e.start_time)
        posted = 0
        skipped = 0
        verified = 0
        only_for_defs = (
            {f"com.fulcradynamics.annotation.{definition_id}"} if definition_id else None
        )
        # Use the events' actual data type for the readback endpoint —
        # otherwise an instant import would read back DurationAnnotation and
        # find nothing, missing the dedup.
        read_data_type = data_type or _default_data_type(events_sorted[0].annotation_type)

        for i in range(0, len(events_sorted), chunk_size):
            chunk = events_sorted[i : i + chunk_size]
            win_start = min(e.start_time for e in chunk) - timedelta(minutes=window_pad_minutes)
            win_end_dt = max((e.end_time or e.start_time) for e in chunk)
            win_end = win_end_dt + timedelta(minutes=window_pad_minutes)

            existing = self.fetch_existing_source_ids(
                win_start, win_end, data_type=read_data_type,
                only_for_defs=only_for_defs,
            )
            new_events = [e for e in chunk if e.source_id not in existing]
            skipped += len(chunk) - len(new_events)

            if new_events:
                self.ingest_batch(
                    new_events, definition_id=definition_id, tag_id_for=tag_id_for,
                    data_type=data_type,
                )
                posted += len(new_events)
                after = self.fetch_existing_source_ids(
                    win_start, win_end, data_type=read_data_type,
                    only_for_defs=only_for_defs,
                )
                verified += sum(1 for e in new_events if e.source_id in after)

        return ImportResult(total=total, skipped_existing=skipped, posted=posted, verified=verified)

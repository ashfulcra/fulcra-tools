"""Fulcra API client + run-import pipeline for fulcra-media-helpers.

Builds on `fulcra_common.BaseFulcraClient` — adds the Watched/Listened/Read
DurationAnnotation definitions, the NormalizedEvent ingest, and the
dedup-readback import pipeline. Auth, the httpx client, tag lookup,
soft-delete, and event readback come from the base.

`ImportResult` is re-exported here so existing
`from fulcra_media.fulcra import ImportResult` imports keep working.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from fulcra_common import BaseFulcraClient, ImportResult
from fulcra_common import wire

from .state import State

if TYPE_CHECKING:
    from .importers.base import NormalizedEvent

__all__ = ["FulcraClient", "ImportResult"]


class FulcraClient(BaseFulcraClient):
    USER_AGENT = "fulcra-media-helpers/0.1"

    def ensure_tag(self, name: str, state: State) -> str:
        """Look up / create a tag, caching the id in `state.tag_ids`."""
        if name in state.tag_ids:
            return state.tag_ids[name]
        tag_id = self._resolve_tag(name)
        state.tag_ids[name] = tag_id
        return tag_id

    def ensure_definitions(self, state: State) -> None:
        if (state.watched_definition_id and state.listened_definition_id
                and state.read_definition_id):
            return
        media = self.ensure_tag("media", state)
        watched = self.ensure_tag("watched", state)
        listened = self.ensure_tag("listened", state)
        read = self.ensure_tag("read", state)

        if not state.watched_definition_id:
            state.watched_definition_id = self._create_duration_definition(
                name="Watched",
                description="Media content watched (movies, TV, video).",
                tags=[media, watched],
            )
        if not state.listened_definition_id:
            state.listened_definition_id = self._create_duration_definition(
                name="Listened",
                description="Media content listened to (music, podcasts).",
                tags=[media, listened],
            )
        if not state.read_definition_id:
            state.read_definition_id = self._create_duration_definition(
                name="Read",
                description="Books read (Goodreads, etc.).",
                tags=[read],
            )

    def _create_duration_definition(self, name: str, description: str, tags: list[str]) -> str:
        body = wire.duration_definition_payload(name=name, description=description, tags=tags)
        r = self._client().post(
            "/user/v1alpha1/annotation",
            json=body,
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        return r.json()["id"]

    def ingest_batch(
        self, events: list["NormalizedEvent"], state: "State"
    ) -> None:
        if not events:
            return
        records: list[dict] = []
        category_to_def = {
            "watched":  state.watched_definition_id,
            "listened": state.listened_definition_id,
            "read":     state.read_definition_id,
        }
        for ev in events:
            def_id = category_to_def.get(ev.category)
            if def_id is None:
                raise RuntimeError(
                    f"missing {ev.category} definition id in state; run bootstrap first"
                )
            data_inner = {
                "note": ev.note,
                "title": ev.title,
                "service": ev.service,
                "timestamp_confidence": ev.timestamp_confidence,
                "external_ids": ev.external_ids,
            }
            service_tag = state.tag_ids.get(ev.service)
            tags = [service_tag] if service_tag else []
            records.append(wire.build_record(
                data_type=wire.DURATION_ANNOTATION,
                start_time=ev.start_time,
                end_time=ev.end_time,
                data=data_inner,
                source_id=ev.deterministic_id,
                tags=tags,
                definition_id=def_id,
            ))
        body = wire.encode_batch(records)
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
        events: "list[NormalizedEvent]",
        state: State,
        chunk_size: int = 500,
        window_pad_minutes: int = 10,
        check_only: bool = False,
    ) -> ImportResult:
        """Run the dedup-readback + ingest pipeline.

        `check_only`: when True, do all the readbacks and dedup math but
        don't POST. Result.posted reports how many *would* post; verified
        stays 0.
        """
        events = list(events)
        total = len(events)
        if total == 0:
            return ImportResult(0, 0, 0, 0)

        # Dedup readback and verification both operate per-chunk on the chunk's
        # own narrow time window. The Fulcra event endpoint has an undocumented
        # pagination ceiling (~4,000 records) and no cursor/limit param, so a
        # single readback over a multi-year window misses records. Per-chunk
        # narrow windows stay well under the ceiling.
        events_sorted = sorted(events, key=lambda e: e.start_time)
        posted = 0
        skipped = 0
        verified = 0

        # Scope dedup readback to the current annotation defs only — events
        # orphaned by a soft-deleted def still surface in queries but their
        # source_id points at the deleted def, so we want to ignore them.
        current_def_source_ids: set[str] = set()
        for def_id in (
            state.watched_definition_id,
            state.listened_definition_id,
            state.read_definition_id,
        ):
            if def_id:
                current_def_source_ids.add(
                    f"com.fulcradynamics.annotation.{def_id}"
                )

        for i in range(0, len(events_sorted), chunk_size):
            chunk = events_sorted[i : i + chunk_size]
            win_start = min(e.start_time for e in chunk) - timedelta(minutes=window_pad_minutes)
            win_end = max(e.end_time for e in chunk) + timedelta(minutes=window_pad_minutes)

            existing = self.fetch_existing_source_ids(
                win_start, win_end, only_for_defs=current_def_source_ids or None
            )
            new_events = [e for e in chunk if e.deterministic_id not in existing]
            skipped += len(chunk) - len(new_events)

            if new_events:
                if check_only:
                    posted += len(new_events)
                else:
                    self.ingest_batch(new_events, state)
                    posted += len(new_events)
                    after = self.fetch_existing_source_ids(
                        win_start, win_end, only_for_defs=current_def_source_ids or None
                    )
                    verified += sum(1 for e in new_events if e.deterministic_id in after)

        # Note: `verified < posted` is no longer fatal. Fulcra accepts the POST
        # (204) but indexing can lag seconds-to-minutes behind for large batches.
        # Callers display the gap so the user knows what's still settling.
        return ImportResult(total=total, skipped_existing=skipped, posted=posted, verified=verified)

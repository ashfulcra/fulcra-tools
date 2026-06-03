"""Fulcra API client + run-import pipeline for fulcra-media-helpers.

Builds on `fulcra_common.BaseFulcraClient` — adds the Watched/Listened/Read
DurationAnnotation definitions, the NormalizedEvent ingest, and the
dedup-readback import pipeline. Auth, the httpx client, tag lookup,
soft-delete, and event readback come from the base.

`ImportResult` is re-exported here so existing
`from fulcra_media.fulcra import ImportResult` imports keep working.
"""

from __future__ import annotations

from collections.abc import Callable
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
        """Post a batch of NormalizedEvents to Fulcra.

        Thin wrapper over `IngestPipeline.ingest_batch`: this method maps
        the importer-side `NormalizedEvent` (which knows about
        watched/listened/read categories and per-service tag ids) onto
        the pipeline-side `DurationEvent`, and delegates the wire
        construction + POST. The legacy inline `wire.build_record +
        encode_batch + httpx.post` block is gone; the `duration_seconds`
        defensive-field injection lives once in the pipeline now.
        """
        if not events:
            return
        from fulcra_common.ingest import IngestPipeline
        pipeline = IngestPipeline(client=self)

        category_to_def = {
            "watched":  state.watched_definition_id,
            "listened": state.listened_definition_id,
            "read":     state.read_definition_id,
        }
        ingestable = []
        for ev in events:
            def_id = category_to_def.get(ev.category)
            if def_id is None:
                raise RuntimeError(
                    f"missing {ev.category} definition id in state; "
                    "run bootstrap first",
                )
            service_tag = state.tag_ids.get(ev.service)
            tag_ids = (service_tag,) if service_tag else ()
            ingestable.append(
                ev.to_duration_event(definition_id=def_id, tags=tag_ids),
            )
        pipeline.ingest_batch(ingestable)

    def run_import(
        self,
        events: "list[NormalizedEvent]",
        state: State,
        chunk_size: int = 500,
        window_pad_minutes: int = 10,
        check_only: bool = False,
        claim: "Callable[[set[str]], bool] | None" = None,
        unclaim: "Callable[[set[str]], None] | None" = None,
    ) -> ImportResult:
        """Run the dedup-readback + ingest pipeline.

        `check_only`: when True, do all the readbacks and dedup math but
        don't POST. Result.posted reports how many *would* post; verified
        stays 0.

        `claim`: an OPTIONAL per-event write-dedup claim, injected by the
        daemon (``ctx.claim_dedup_keys``) so media-helpers never imports
        ``fulcra-collect``. For each event that SURVIVES the readback-skip,
        ``claim`` is called with that event's full dedup-key set
        (``{deterministic_id} ∪ extra_source_ids``); the event is POSTed
        only when ``claim`` returns ``True``. This closes the window the
        readback alone can't: two concurrent runs, or two cross-source
        twins in the SAME run that share a ``com.fulcra.content.*``
        fingerprint, would each pass the readback (neither is in Fulcra
        yet) — the claim, an atomic INSERT OR IGNORE against a shared
        PRIMARY KEY, lets exactly one of them through. A claimed-but-skipped
        event counts as ``skipped_existing`` (it's a duplicate of a sibling
        being written this very run). When ``claim is None`` (standalone CLI
        imports outside the daemon) behaviour is exactly as before:
        readback-skip only, no per-event claim.

        `unclaim`: the OPTIONAL inverse of ``claim``, injected alongside it
        (``ctx.unclaim_dedup_keys``). Claims are taken BEFORE the batch POST
        so a concurrent run is blocked during the POST window — but a media
        annotation is durable timeline data, so if the POST RAISES we must
        release the keys we just claimed for that batch, or those events are
        skipped forever and silently lost. On a POST failure we unclaim
        exactly the keys this batch newly claimed (NOT pre-existing rows or
        rows from other batches/events) and re-raise, so the next run retries
        them. On POST success the claims stay. When ``unclaim is None`` the
        keys are left claimed on failure (the pre-fix behaviour) — only the
        daemon path supplies it, and only it has durable-loss exposure.
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
            # Skip an event if ANY of its dedup keys is already present — the
            # per-plugin deterministic_id OR a cross-source content fingerprint
            # (com.fulcra.content.*.v1.<hash>) carried in extra_source_ids. The
            # fingerprints are already in `existing` via fetch_existing_source_ids
            # (it reads both the sources and metadata.source arrays). An event
            # with no extra_source_ids therefore behaves exactly as before:
            # skip iff deterministic_id in existing.
            new_events = [
                e for e in chunk
                if not ({e.deterministic_id, *e.extra_source_ids} & existing)
            ]
            skipped += len(chunk) - len(new_events)

            # Per-event write-dedup claim (component 3). For each event that
            # survived the readback-skip, atomically claim its full dedup-key
            # set; POST only the events whose claim succeeded. A claim that
            # returns False means a concurrent run — or a cross-source twin
            # earlier in THIS chunk's iteration — already claimed one of the
            # event's keys, so this event would be a duplicate: count it as
            # skipped and don't POST it. ``check_only`` is a dry run and must
            # not mutate the shared dedup store, so the claim is bypassed
            # there (matching the "don't POST" semantics of check_only).
            # Keys THIS batch newly inserted into forwarded_events — the only
            # ones we may release if the POST fails. Built from the events
            # whose claim returned True, so it never includes pre-existing
            # rows or keys owned by a different event/batch.
            newly_claimed_keys: set[str] = set()
            if claim is not None and not check_only:
                claimed_events = []
                for e in new_events:
                    keyset = {e.deterministic_id, *e.extra_source_ids}
                    if claim(keyset):
                        claimed_events.append(e)
                        newly_claimed_keys |= keyset
                skipped += len(new_events) - len(claimed_events)
                new_events = claimed_events

            if new_events:
                if check_only:
                    posted += len(new_events)
                else:
                    try:
                        self.ingest_batch(new_events, state)
                    except Exception:
                        # Durable-loss guard: the POST failed, so release the
                        # claims this batch took (scoped to keys WE newly
                        # inserted) and let the next run retry these events.
                        # Without this they'd be skipped forever. Best-effort
                        # — never let an unclaim error mask the original POST
                        # failure, which is the one the caller must see.
                        if unclaim is not None and newly_claimed_keys:
                            try:
                                unclaim(newly_claimed_keys)
                            except Exception:
                                pass
                        raise
                    posted += len(new_events)
                    after = self.fetch_existing_source_ids(
                        win_start, win_end, only_for_defs=current_def_source_ids or None
                    )
                    verified += sum(1 for e in new_events if e.deterministic_id in after)

        # Note: `verified < posted` is no longer fatal. Fulcra accepts the POST
        # (204) but indexing can lag seconds-to-minutes behind for large batches.
        # Callers display the gap so the user knows what's still settling.
        return ImportResult(total=total, skipped_existing=skipped, posted=posted, verified=verified)

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
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

from fulcra_common import BaseFulcraClient, ImportResult
from fulcra_common import wire

from .state import State

if TYPE_CHECKING:
    from .importers.base import NormalizedEvent

__all__ = ["FulcraClient", "ImportResult"]

_MEDIA_NAMESPACE = "com.fulcra.media."


def _importer_namespace_prefix(det_id: str) -> str | None:
    """Per-importer namespace prefix of a deterministic_id, or None.

    Importer det-ids share the shape ``com.fulcra.media.<importer>.<rest>``
    where ``<rest>`` varies across importers: ``v1.<sha16>`` (lastfm,
    deezer, spotify-extended, ...), ``v2.<sha16>`` (netflix), a bare
    ``<sha16>`` with NO version segment (netflix-rich), ``v1.history.<id>``
    (trakt), ``v1.<service id>`` (strava). The one stable invariant is the
    importer segment immediately after ``com.fulcra.media.`` — so the
    prefix is derived as ``com.fulcra.media.<importer>.`` rather than by
    stripping a trailing ``v1.<hash>``, which several importers don't have.

    Returns None for anything that doesn't fit the shape; callers fall
    back to the conservative skip-on-fingerprint-match behaviour.
    """
    if not det_id.startswith(_MEDIA_NAMESPACE):
        return None
    rest = det_id[len(_MEDIA_NAMESPACE):]
    importer_seg, _, tail = rest.partition(".")
    if not importer_seg or not tail:
        return None
    return f"{_MEDIA_NAMESPACE}{importer_seg}."


def _without_sources(e: "NormalizedEvent", drop: set[str]) -> "NormalizedEvent":
    """Copy of ``e`` with ``drop`` removed from extra_source_ids.

    A COPY, never an in-place mutation: callers of run_import reuse their
    event list afterwards (record_twins_after_post → record_imported_events
    reads external_ids / start_time off the same objects), so the originals
    must stay intact.
    """
    return replace(
        e,
        extra_source_ids=tuple(s for s in e.extra_source_ids if s not in drop),
    )


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
        ``claim`` is called with that event's dedup-key set
        (``{deterministic_id} ∪ extra_source_ids``); the event is POSTed
        only when ``claim`` returns ``True``. This closes the window the
        readback alone can't: two concurrent runs sharing a key would each
        pass the readback (neither is in Fulcra yet) — the claim, an atomic
        INSERT OR IGNORE against a shared PRIMARY KEY, lets exactly one of
        them through. Fingerprints already claimed EARLIER IN THIS RUN are
        stripped from the event before its claim: a run is a single plugin,
        so a same-run fingerprint collision is a same-source quick replay
        (two real plays in one 5-minute bucket), not a cross-source twin —
        the replay still posts, claiming its remaining keys. A
        claimed-but-skipped event counts as ``skipped_existing``. When
        ``claim is None`` (standalone CLI imports outside the daemon)
        behaviour is exactly as before: readback-skip only, no per-event
        claim.

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

        # Dedup keys claimed by THIS run so far (across chunks). A run is a
        # single plugin, so a fingerprint-claim collision against a key in
        # this set is a same-source quick replay BY CONSTRUCTION — see the
        # claim block below.
        run_claimed_keys: set[str] = set()

        for i in range(0, len(events_sorted), chunk_size):
            chunk = events_sorted[i : i + chunk_size]
            win_start = min(e.start_time for e in chunk) - timedelta(minutes=window_pad_minutes)
            win_end = max(e.end_time for e in chunk) + timedelta(minutes=window_pad_minutes)

            existing_groups = self.fetch_existing_source_groups(
                win_start, win_end, only_for_defs=current_def_source_ids or None
            )
            existing: set[str] = set()
            for grp in existing_groups:
                existing |= grp

            # Readback dedup, per event:
            #   1. deterministic_id already present → true duplicate → skip.
            #   2. A cross-source content fingerprint
            #      (com.fulcra.content.*.v1.<hash>) in extra_source_ids
            #      matches an existing record R. Two very different things
            #      hash to the same fingerprint because it buckets timestamps
            #      to 5 minutes:
            #        - same listen reported by TWO services (cross-source
            #          twin) → skip, as before;
            #        - the SAME service reporting two real plays inside one
            #          bucket (a quick replay — e.g. Last.fm, "Born to Be
            #          Alive" played at 15:00:17 and again at 15:03:36 on
            #          2026-06-07; the old key-intersection skip silently
            #          dropped the second play). When EVERY existing record
            #          claiming the fingerprint also carries a source id
            #          from this event's own importer namespace, it's a
            #          replay: POST it, with the matched fingerprint STRIPPED
            #          from the wire source array — otherwise query-time
            #          source-merging would collapse the replay into the
            #          original record. The strip happens on a COPY; the
            #          caller's events are never mutated.
            #      If the importer prefix can't be derived, or any claiming
            #      record is from a different importer, fall back to the
            #      conservative skip.
            #   An event with no extra_source_ids behaves exactly as before:
            #   skip iff deterministic_id in existing.
            new_events = []
            for e in chunk:
                if e.deterministic_id in existing:
                    continue  # true duplicate
                matched_fps = {f for f in e.extra_source_ids if f in existing}
                if not matched_fps:
                    new_events.append(e)
                    continue
                prefix = _importer_namespace_prefix(e.deterministic_id)
                same_source = prefix is not None and all(
                    any(src.startswith(prefix) for src in grp)
                    for grp in existing_groups
                    if grp & matched_fps
                )
                if same_source:
                    new_events.append(_without_sources(e, matched_fps))
                # else: cross-source twin (or unprovable) → skip.
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
                    # Batch-internal same-source replay: a run is a single
                    # plugin, so a fingerprint claimed EARLIER IN THIS RUN
                    # belongs to a sibling event from the same importer —
                    # by construction a quick replay, never a cross-source
                    # twin. Strip those fingerprints (on a copy) up front
                    # and claim only the remaining keys; the det_id must
                    # still claim successfully or the event is a true dup.
                    already_ours = set(e.extra_source_ids) & run_claimed_keys
                    if already_ours:
                        e = _without_sources(e, already_ours)
                    keyset = {e.deterministic_id, *e.extra_source_ids}
                    if claim(keyset):
                        claimed_events.append(e)
                        newly_claimed_keys |= keyset
                        run_claimed_keys |= keyset
                    # else: conservative edge, OLD behaviour preserved. The
                    # collision is on the det_id (true dup / concurrent run)
                    # or on a fingerprint claimed by a CONCURRENT process —
                    # one not claimed by this run and not visible in the
                    # readback grouping, so we cannot prove it's same-source.
                    # Skipping here can still drop a genuine replay in the
                    # narrow window where the original play was forwarded but
                    # hasn't surfaced in the readback yet; we accept that
                    # over risking a cross-source double-write.
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

"""Fulcra API client + run-import pipeline for fulcra-media-helpers.

Builds on `fulcra_common.BaseFulcraClient` — adds the Watched/Listened/Read
DurationAnnotation definitions, the NormalizedEvent ingest, and the
dedup-readback import pipeline. Auth, the httpx client, tag lookup,
soft-delete, and event readback come from the base.

`ImportResult` is re-exported here so existing
`from fulcra_media.fulcra import ImportResult` imports keep working.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fulcra_common import BaseFulcraClient, ImportResult
from fulcra_common import wire

from .state import State

if TYPE_CHECKING:
    from .importers.base import NormalizedEvent

__all__ = ["FulcraClient", "ImportResult"]

_log = logging.getLogger("fulcra_media.fulcra")

_MEDIA_NAMESPACE = "com.fulcra.media."

# The dedup-readback pre-gate (GET /data/v1/updates before the per-chunk
# event readback) only runs when the chunk's window starts within this span
# of now. Two live findings force the bound (2026-07-06):
#   * data_updates is PROCESSING-time based, so the sound gate window is
#     [win_start, now] — for a historical chunk (2020 event times) that
#     span covers years, and
#   * the endpoint 500s on large windows (a 7-day range failed live).
# Live polled imports — the case where the readback is usually pointless —
# have win_start within minutes of now, so they gate; historical bulk
# imports skip the gate entirely and pay the normal readback.
_UPDATES_GATE_MAX_SPAN = timedelta(hours=48)

# Delayed landed-count verification for the typed ingest endpoint. The
# typed POST returns 201 and processes ASYNC (~1-2 min observed), and a
# JSONL batch containing a bad line returns 201 while SILENTLY dropping
# that line — no per-line error, no upload-status endpoint (all
# live-verified 2026-07-08). The immediate post-POST readback therefore
# usually misses fresh records; run_import re-polls records not yet
# visible up to _LANDED_VERIFY_ATTEMPTS times, sleeping
# _LANDED_VERIFY_DELAY_S before EACH attempt (the first attempt is also
# delayed — nothing is visible sooner). 4 x 30 s covers the observed lag;
# a record slower than that is unclaimed and self-heals on the next run.
_LANDED_VERIFY_ATTEMPTS = 4
_LANDED_VERIFY_DELAY_S = 30.0


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

    def ingest_batch_typed(
        self, events: list["NormalizedEvent"], state: "State"
    ) -> None:
        """Typed-ingest counterpart of ``ingest_batch``, used by ``run_import``.

        Maps each NormalizedEvent onto its IngestableEvent (via
        ``to_duration_event`` — media events are all durations today),
        builds an UNWRAPPED record with ``wire.build_typed_record`` (note =
        the event's note; the det source_id + cross-source content
        fingerprints via ``extra_sources``; the definition binding via
        ``definition_id``), groups by base type (MomentEvent →
        MomentAnnotation, DurationEvent → DurationAnnotation), and posts
        each group via ``IngestPipeline.ingest_typed``. The source-array
        composition is identical to what ``ingest_batch``/``build_record``
        produced — [source_id, *extra_sources, definition-source] — so the
        dedup readback still matches.

        The typed endpoint has NO server-side source-id dedup and returns
        201 for a JSONL batch even when it SILENTLY drops a bad line
        (live-verified 2026-07-08): ``run_import``'s claim + readback
        machinery is the required dedup compensation and is left untouched,
        and its landed-count WARNING surfaces any silent drop. The legacy
        ``ingest_batch`` stays for the webhook receiver (no rug-pull).

        The wrapped ``data`` payload the legacy path carried (title,
        service, timestamp_confidence, external_ids, duration_seconds) has
        NO typed slot and is dropped from the wire — nothing reads those
        back from the server: external_ids was never surfaced on event
        queries (twin_cache.py exists precisely because of that), and
        title/service/timestamp_confidence are read only off the in-memory
        events pre/post-POST (health checks, cli), never re-queried. So no
        queryable state is lost; ``note`` is the only free-form slot the
        typed schema offers.
        """
        if not events:
            return
        from fulcra_common.ingest import (
            DurationEvent, IngestPipeline, MomentEvent,
        )
        pipeline = IngestPipeline(client=self)

        category_to_def = {
            "watched":  state.watched_definition_id,
            "listened": state.listened_definition_id,
            "read":     state.read_definition_id,
        }
        groups: dict[str, list[dict]] = {}
        for ev in events:
            def_id = category_to_def.get(ev.category)
            if def_id is None:
                raise RuntimeError(
                    f"missing {ev.category} definition id in state; "
                    "run bootstrap first",
                )
            service_tag = state.tag_ids.get(ev.service)
            tag_ids = (service_tag,) if service_tag else ()
            ingestable = ev.to_duration_event(
                definition_id=def_id, tags=tag_ids,
            )
            if isinstance(ingestable, DurationEvent):
                base_type = wire.DURATION_ANNOTATION
                record = wire.build_typed_record(
                    base_type=base_type,
                    start_time=ingestable.start,
                    end_time=ingestable.end,
                    note=ingestable.note,
                    source_id=ingestable.source_id,
                    definition_id=ingestable.definition_id,
                    extra_sources=ingestable.extra_source_ids,
                    tags=ingestable.tags,
                )
            elif isinstance(ingestable, MomentEvent):
                base_type = wire.MOMENT_ANNOTATION
                record = wire.build_typed_record(
                    base_type=base_type,
                    start_time=ingestable.ts,
                    note=ingestable.note,
                    source_id=ingestable.source_id,
                    definition_id=ingestable.definition_id,
                    extra_sources=ingestable.extra_source_ids,
                    tags=ingestable.tags,
                )
            else:  # pragma: no cover - defensive; only two subclasses exist
                raise TypeError(
                    "unknown IngestableEvent subclass: "
                    f"{type(ingestable).__name__}"
                )
            groups.setdefault(base_type, []).append(record)

        for base_type, records in groups.items():
            pipeline.ingest_typed(base_type, records)

    def run_import(
        self,
        events: "list[NormalizedEvent]",
        state: State,
        chunk_size: int = 500,
        window_pad_minutes: int = 10,
        check_only: bool = False,
        claim: "Callable[[set[str]], bool] | None" = None,
        unclaim: "Callable[[set[str]], None] | None" = None,
        updates_summary: "Callable[[datetime, datetime], dict[str, int]] | None" = None,
        sleep_fn: "Callable[[float], None] | None" = None,
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

        `updates_summary`: OPTIONAL injectable seam (same style as
        ``claim``/``unclaim``) for the dedup-readback PRE-GATE. Before each
        chunk's ``fetch_existing_source_groups`` readback, the pipeline asks
        ``GET /data/v1/updates`` (via ``BaseFulcraClient.data_updates_summary``
        when no callable is injected) whether ANY record of the chunk's data
        type was PROCESSED between the chunk's window start and now. Zero →
        the readback cannot match anything (a record with an event time in
        the window must have been processed after that event started), so
        the readback fetch is skipped and every event is treated as new —
        the per-event ``claim`` still runs, so the write-dedup guarantee is
        untouched. Non-zero, or any error from the updates call → the normal
        readback runs (fail-open: gating failure must NEVER block or
        de-safe an import). The gate only engages when ``win_start`` is
        within ``_UPDATES_GATE_MAX_SPAN`` of now — data_updates is
        processing-time based (verified live 2026-07-06), so the sound gate
        window for an old chunk would span years and the endpoint 500s on
        large windows. Injected callables receive ``(window_start,
        window_end)`` datetimes and return ``{data_type: processed_count}``.

        `sleep_fn`: injectable sleeper (tests) for the run-end delayed
        landed-count verification; defaults to ``time.sleep``. The delayed
        verify + self-healing unclaim (see the block after the chunk loop)
        exists because the typed endpoint pairs badly with persistent
        claims: a 201'd-but-silently-dropped JSONL line whose claim is
        never released would be skipped by every future run — permanent
        loss the legacy synchronous, server-deduped path could not
        produce.
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

        # POSTed-but-not-yet-visible bookkeeping for the run-end delayed
        # verification: one entry per chunk that posted, carrying the
        # chunk's readback window and, per still-missing event, the exact
        # dedup keys ITS claim inserted (so a self-healing unclaim releases
        # precisely those). Only populated on claim runs — without a claim
        # store nothing blocks a retry (the next run's readback finds no
        # record and re-posts), so there is nothing to heal.
        pending_verify: list[tuple[datetime, datetime, dict[str, set[str]]]] = []

        # The readback (and therefore the pre-gate) is over DurationAnnotation
        # records: run_import ingests every NormalizedEvent category through
        # to_duration_event, and fetch_existing_source_groups below queries
        # the same type. Keep the two keyed on ONE name so they can't drift.
        readback_data_type = "DurationAnnotation"

        for i in range(0, len(events_sorted), chunk_size):
            chunk = events_sorted[i : i + chunk_size]
            win_start = min(e.start_time for e in chunk) - timedelta(minutes=window_pad_minutes)
            win_end = max(e.end_time for e in chunk) + timedelta(minutes=window_pad_minutes)

            # Dedup-readback pre-gate: if zero records of the readback's data
            # type were PROCESSED between win_start and now, no record with an
            # event time inside this chunk's window can exist (a matching
            # record — a prior import of a past play — is always processed
            # after its event started), so the readback would return nothing.
            # Skip it and treat `existing` as empty. Failure of the updates
            # call, a non-zero count, or a window too old to gate soundly →
            # the normal readback (fail-open in every direction).
            skip_readback = False
            gate_now = datetime.now(timezone.utc)
            if timedelta(0) <= gate_now - win_start <= _UPDATES_GATE_MAX_SPAN:
                summary_fn = (updates_summary if updates_summary is not None
                              else self.data_updates_summary)
                try:
                    counts = summary_fn(win_start, max(win_end, gate_now))
                    skip_readback = counts.get(readback_data_type, 0) == 0
                except Exception:  # noqa: BLE001 — never let gating block an import
                    skip_readback = False

            if skip_readback:
                existing_groups: list[set[str]] = []
            else:
                existing_groups = self.fetch_existing_source_groups(
                    win_start, win_end, only_for_defs=current_def_source_ids or None,
                    data_type=readback_data_type,
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
            # Per-event claim keys for this chunk — feeds the run-end
            # delayed verification's self-healing unclaim.
            chunk_event_keys: dict[str, set[str]] = {}
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
                        chunk_event_keys[e.deterministic_id] = keyset
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
                        self.ingest_batch_typed(new_events, state)
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
                    verified += sum(
                        1 for e in new_events if e.deterministic_id in after
                    )
                    # Records the immediate readback did not see are the
                    # NORMAL case on the typed endpoint (201 = queued,
                    # processing is async ~1-2 min; live-verified
                    # 2026-07-08) — no warning here. On claim runs they
                    # enter the run-end delayed verification below, which
                    # is where a genuine silent drop becomes visible and
                    # self-heals. On claimless runs (standalone CLI) the
                    # verified<posted gap in ImportResult is the caller's
                    # signal, and a dropped record re-posts naturally on
                    # the next run.
                    if claim is not None:
                        chunk_missing = {
                            e.deterministic_id:
                                chunk_event_keys.get(e.deterministic_id, set())
                            for e in new_events
                            if e.deterministic_id not in after
                        }
                        if chunk_missing:
                            pending_verify.append(
                                (win_start, win_end, chunk_missing))

        # Run-end delayed verification + self-healing unclaim (claim runs
        # only). Why it exists: the typed endpoint returns 201 with async
        # processing AND silently drops a bad JSONL line (no per-line
        # error, no upload-status endpoint; live-verified 2026-07-08),
        # while `unclaim` above fires only on a POST *failure* — so a
        # dropped line after a 201 would leave its claim held forever and
        # the event skipped by every future run: permanent loss the legacy
        # synchronous, server-deduped path could not produce. The fix:
        # re-poll the still-missing ids over each chunk's own narrow
        # readback window (the event endpoint's ~4k pagination ceiling
        # rules out one wide query), bounded to _LANDED_VERIFY_ATTEMPTS x
        # _LANDED_VERIFY_DELAY_S with the first attempt delayed too.
        if pending_verify and not check_only:
            sleep = sleep_fn if sleep_fn is not None else time.sleep
            recovered = 0
            verify_error: Exception | None = None
            for _attempt in range(_LANDED_VERIFY_ATTEMPTS):
                sleep(_LANDED_VERIFY_DELAY_S)
                try:
                    for vwin_start, vwin_end, missing in pending_verify:
                        if not missing:
                            continue
                        found = self.records_visible(
                            readback_data_type, set(missing),
                            vwin_start, vwin_end,
                        )
                        for det_id in found:
                            del missing[det_id]
                        recovered += len(found)
                        verified += len(found)
                except Exception as exc:  # noqa: BLE001 — fail-safe branch below
                    verify_error = exc
                    break
                if not any(missing for _, _, missing in pending_verify):
                    break

            still_missing: dict[str, set[str]] = {}
            for _, _, missing in pending_verify:
                still_missing.update(missing)

            if verify_error is not None:
                # Fail-safe: do NOT unclaim on an unverifiable outcome.
                # Keeping the claim loses the event only if it was ALSO
                # silently dropped; unclaiming on unknown risks a duplicate
                # write (the typed endpoint has no server-side source-id
                # dedup). Prefer the rarer loss.
                _log.warning(
                    "typed ingest: could not verify landings for %d posted "
                    "record(s) (readback failed: %s); leaving dedup claims "
                    "in place — unclaiming on an unknown outcome risks "
                    "duplicates (no server-side dedup)",
                    len(still_missing), verify_error,
                )
            elif still_missing:
                # Genuine silent drop — or indexing lag beyond the poll
                # bound, in which case the unclaim makes the next run's
                # readback find the (by then visible) record and skip it,
                # so this self-heals either way.
                if unclaim is not None:
                    try:
                        unclaim(set().union(*still_missing.values()))
                    except Exception:  # noqa: BLE001 — never mask the warning
                        pass
                heal = ("dedup keys unclaimed for retry next run"
                        if unclaim is not None
                        else "no unclaim hook — keys remain claimed")
                _log.warning(
                    "typed ingest: %d record(s) still not visible %.0f s "
                    "after POST; missing source-ids: %s — %s (a "
                    "silently-dropped JSONL line, or indexing lag beyond "
                    "the poll bound, which also self-heals next run)",
                    len(still_missing),
                    _LANDED_VERIFY_ATTEMPTS * _LANDED_VERIFY_DELAY_S,
                    ", ".join(sorted(still_missing)), heal,
                )
            elif recovered:
                # Everything landed during the poll — the expected async
                # lag, nothing above DEBUG.
                _log.debug(
                    "typed ingest: %d record(s) became visible during the "
                    "delayed verification", recovered,
                )

        # Note: `verified < posted` is no longer fatal. Fulcra accepts the POST
        # (201, async processing) but indexing can lag behind the delayed
        # verification's bound for large batches. Callers display the gap so
        # the user knows what's still settling.
        return ImportResult(total=total, skipped_existing=skipped, posted=posted, verified=verified)

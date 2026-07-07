"""Helpers shared by two or more per-plugin modules.

Anything used by only one plugin lives in that plugin's module; this file
is for the genuinely shared bits (definition resolution, watermark
arithmetic, the canonical Duration spec shape, plus the constant maps
used by generic-rss and generic-csv).

Note on monkeypatching: each per-plugin module imports ``FulcraClient`` /
``_state_load`` / ``library`` / its importer directly so that test
monkeypatches at ``fulcra_media.plugins.<id>.<symbol>`` replace the names
actually read by the plugin's run function. The shared scheduled-import
and rss-import helpers below take the same bindings as arguments rather
than reading them from their own module globals — that's why each
per-plugin module passes ``fulcra_client_cls=FulcraClient,
state_load=_state_load`` from its own scope.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fulcra_collect.plugin import RunContext
from fulcra_csv import find_low_conf_twins

from .. import twin_cache
from ..state import DEFAULT_PATH as STATE_PATH


# The Fulcra annotation definition shape for every typed-media DurationAnnotation
# (Watched / Listened / Read). All plugins share this exact structure; only the
# canonical name passed to the resolver differs. The per-plugin modules each
# alias this under a self-documenting <PLUGIN>_<KIND>_SPEC name so the resolver
# call sites stay readable.
DURATION_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


# Maps the runtime config category to the canonical Fulcra definition name.
# Used by generic-rss and generic-csv where the canonical identity is chosen
# at run-time from plugin config rather than baked into the Plugin definition.
CATEGORY_TO_CANONICAL: dict[str, str] = {
    "watched": "Watched",
    "listened": "Listened",
    "read": "Read",
}


def ensure_media_def(ctx: RunContext, media_state, *,
                     attr: str, spec: dict, canonical_name: str,
                     state_save) -> str:
    """Get/refresh a canonical definition id stored on the shared media
    state file. Wraps ``ctx.ensure_definition`` to also write the new id
    back to per-package state when it changes.

    ``state_save`` is passed in by the caller (rather than imported here)
    so the per-plugin module's binding wins — monkeypatching
    ``fulcra_media.plugins.<id>._state_save`` will flow through.

    Replaces the older ``if not media_state.<attr>: resolve; save`` pattern
    that trusted the per-package cache blindly across daemon re-auths
    to a different Fulcra account — the same orphan-ingest hazard task
    #12 fixed for the attention plugin. See [[task #13]] for the
    generalisation and [[task #16]] for the tag_ids parity.

    When the def is re-resolved (cached value was stale on the current
    account), also invalidate the tag_ids dict — those tag UUIDs were
    populated alongside the now-orphan def and almost certainly belong
    to the prior account too. ensure_tag will repopulate fresh UUIDs
    from the current account on next access. Costs O(N) round-trips
    once, only after an account switch.
    """
    cached = getattr(media_state, attr, None)
    def_id = ctx.ensure_definition(
        cached=cached, expected_spec=spec, canonical_name=canonical_name,
    )
    if cached != def_id:
        setattr(media_state, attr, def_id)
        # Stale-def detected → tag cache is also suspect on an account
        # switch. Clear only when ``cached`` was truthy (i.e. we had a
        # cache and it was wrong) — first-run resolves shouldn't pay
        # the tag-rebuild cost.
        if cached and hasattr(media_state, "tag_ids"):
            media_state.tag_ids = {}
        state_save(media_state)
    return def_id


def newest_event_iso(events: list) -> str | None:
    """The newest start_time across ``events``, as an ISO string — the new
    watermark. None when there are no events."""
    if not events:
        return None
    return max(e.start_time for e in events).isoformat()


def since_from_watermark(ctx: RunContext) -> datetime | None:
    """Return watermark - 24h as a tz-aware datetime, or None for full backfill.

    The rewind hedges against LATE-ARRIVING scrobbles, not just server-side
    reordering: a phone that listened offline uploads its plays to Last.fm
    when the app next foregrounds — hours late. The original 1-hour rewind
    permanently missed those (confirmed live 2026-06-07: a 15:38 scrobble
    uploaded late never landed in Fulcra while its on-time 14:34 sibling
    did). 24h covers the dominant late-upload case; det-id readback in the
    ingest layer discards the re-fetched duplicates, and the wider window
    costs at most ~one extra API page per run at scrobble volumes. Used by
    Last.fm- and Deezer-shaped scheduled plugins.
    """
    if not ctx.state.watermark:
        return None
    return datetime.fromisoformat(
        ctx.state.watermark.replace("Z", "+00:00")
    ) - timedelta(hours=24)


# ---------------------------------------------------------------------------
# Cross-source twin dedup on the scheduled path (Component 4).
#
# The local high-confidence twin cache lets a LOW-confidence incoming event
# defer to a cached HIGH-confidence twin of the same content (same
# content_fingerprint) imported on a previous run — the kind of match the
# exact-bucket .content.* dedup key can't catch because confidence and
# timestamps differ. Both halves below run for EVERY scheduled / file / RSS
# plugin that routes through this shared glue, mirroring what the CLI import
# path does in cli_common.run_and_emit + cli._maybe_apply_twin_dedup. Trakt
# keeps its own inline copy of this logic (it predates the shared seam and
# carries extra cluster-policy handling); the behaviour is identical.
# ---------------------------------------------------------------------------

def apply_twin_policy(
    events: list,
    *,
    twin_policy: str,
    cached_pool: list,
) -> list:
    """Consult the twin cache and apply the configured twin policy to ``events``.

    ``twin_policy`` mirrors the CLI / trakt non-interactive branches:
      - "keep" (DEFAULT): no-op. The machinery is consulted only when a
        non-default policy is set, so the steady-state behaviour is unchanged.
      - "auto-discard": drop any low-conf incoming event whose
        content_fingerprint matches a high-conf entry in ``cached_pool``.
      - "ask": interactive — unsupported headlessly, raises RuntimeError so the
        failure is loud rather than a silent skip (matches trakt's guard).

    ``cached_pool`` is the already-ingested high-conf pool (from
    ``twin_cache.load_for_twin_lookup()``); passed in so callers can inject a
    test pool and so the load happens once per run.
    """
    if twin_policy == "keep":
        return events
    if twin_policy == "ask":
        raise RuntimeError(
            "twin_policy 'ask' is interactive — set it to auto-discard or "
            "keep in the plugin config for scheduled/headless runs"
        )
    if twin_policy != "auto-discard":
        raise RuntimeError(
            f"unknown twin_policy {twin_policy!r} — must be auto-discard or keep"
        )
    pairs = find_low_conf_twins(events, extra_pool=cached_pool)
    if not pairs:
        return events
    to_drop = {twin_cache._source_id_of(low) for low, _high in pairs}
    # Filter on the same id accessor that built ``to_drop``. fulcra_csv's
    # apply_twin_decisions reads ``e.source_id`` only, but media-helpers'
    # NormalizedEvent exposes its dedup id as ``deterministic_id``; the cross-
    # name accessor _source_id_of handles both, keeping the scheduled path
    # correct for real NormalizedEvents (GenericEvent already matches both).
    return [e for e in events if twin_cache._source_id_of(e) not in to_drop]


def consult_twin_cache(
    ctx: RunContext, events: list, *, default_twin_policy: str = "keep"
) -> list:
    """Load the twin cache and apply ctx's configured twin policy to ``events``.

    The single seam scheduled plugins call before importing. ``twin_policy`` is
    resolved from ``ctx.config`` first, then the plugin-supplied
    ``default_twin_policy`` — so operator config always wins, and a plugin can
    opt its whole class of imports into "auto-discard" (e.g. apple-tv, netflix,
    apple-takeout, which routinely re-observe watches a realtime source like
    Trakt already recorded at high confidence). The global fallback stays
    "keep" so unmigrated plugins are behaviour-preserving.
    """
    twin_policy = ctx.config.get("twin_policy", default_twin_policy)
    if twin_policy == "keep":
        # Fast path: no need to touch the cache for the no-op default.
        return events
    cached = twin_cache.load_for_twin_lookup()
    return apply_twin_policy(events, twin_policy=twin_policy, cached_pool=cached)


def record_twins_after_post(events: list, *, posted: int) -> None:
    """Populate the twin cache with this run's high-confidence events — but
    only after a successful POST (``posted`` > 0), so we never cache an event
    that wasn't actually written to Fulcra. Mirrors cli_common.run_and_emit.

    ``record_imported_events`` itself filters to high-confidence events that
    carry a content_fingerprint, so we pass the full batch. Cache failures
    must never break an import, so we swallow and log.
    """
    if posted <= 0:
        return
    try:
        twin_cache.record_imported_events(events)
    except Exception:  # pragma: no cover - defensive; cache is best-effort
        import logging
        logging.getLogger("fulcra_media.twin_cache").warning(
            "twin cache write failed after scheduled import", exc_info=True
        )


def run_scheduled_import(
    ctx: RunContext,
    *,
    fetch,
    normalize,
    tag: str,
    fulcra_client_cls,
    state_load,
    newest_iso=newest_event_iso,
) -> None:
    """Shared tail for simple fetch-normalize-import-advance scheduled plugins.

    Calls ``fetch(since)`` → ``normalize(raw)`` → ensure_tag + run_import →
    advances ctx.state.watermark to the newest processed event whenever the
    import completes — both posted and skipped-existing count as progress, so
    the all-duplicate steady state created by the 1-hour rewind window does
    not freeze the watermark.

    ``fulcra_client_cls`` / ``state_load`` are passed by the caller so the
    per-plugin module's bindings win, letting tests monkeypatch
    ``fulcra_media.plugins.<id>.FulcraClient`` and have the patch take effect
    here. ``newest_iso`` is similarly an injection seam — test_lastfm patches
    ``newest_event_iso`` in two places to simulate the empty-events branch.
    """
    since = since_from_watermark(ctx)
    raw = list(fetch(since))
    events = list(normalize(raw))
    ctx.progress(stage="fetched", count=len(events))

    # Cross-source twin dedup: consult the local high-conf twin cache and apply
    # the configured twin policy (default "keep" → no-op) before importing.
    events = consult_twin_cache(ctx, events)

    media_state = state_load(STATE_PATH)
    client = fulcra_client_cls()
    client.ensure_tag(tag, media_state)
    result = client.run_import(events, media_state, claim=ctx.claim_dedup_keys,
                               unclaim=ctx.unclaim_dedup_keys)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    if result.posted > 0:
        ctx.annotation(
            f"{tag.capitalize()}: {result.posted} new annotation"
            + ("s" if result.posted != 1 else ""),
            ok=True,
        )

    # Populate the twin cache with this run's high-conf events (after a
    # successful POST only), so a future low-conf twin can defer to them.
    record_twins_after_post(events, posted=result.posted)

    # Advance even when posted == 0: every event in ``events`` was either posted
    # OR skipped-as-already-in-Fulcra — both count as successfully processed.
    # Gating on posted > 0 froze the watermark indefinitely in the all-duplicate
    # steady state created by the 1-hour rewind window above.
    new_wm = newest_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm


def resolve_path(ctx: RunContext, library_mod) -> Path:
    """Read ``path`` from ctx.config, raising RuntimeError if absent.

    ``library_mod`` is the per-plugin module's ``library`` binding so tests
    can monkeypatch ``fulcra_media.plugins.<id>.library.resolve`` and have
    the patch flow through here.
    """
    raw = ctx.config.get("path")
    if not raw:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'path' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    return library_mod.resolve(raw)


def import_events(
    ctx: RunContext,
    events: list,
    tag: str,
    *,
    fulcra_client_cls,
    state_load,
    default_twin_policy: str = "keep",
) -> None:
    """Run the standard ensure_tag + run_import pipeline and report progress."""
    ctx.progress(stage="parsed", count=len(events))

    # Cross-source twin dedup. ``default_twin_policy`` lets a plugin opt in
    # (apple-tv/netflix/apple-takeout pass "auto-discard"); operator config
    # still overrides. Global default stays "keep" → no-op for other plugins.
    events = consult_twin_cache(ctx, events,
                                default_twin_policy=default_twin_policy)

    media_state = state_load(STATE_PATH)
    client = fulcra_client_cls()
    client.ensure_tag(tag, media_state)
    result = client.run_import(events, media_state, claim=ctx.claim_dedup_keys,
                               unclaim=ctx.unclaim_dedup_keys)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    if result.posted > 0:
        ctx.annotation(
            f"{tag.capitalize()}: {result.posted} new annotation"
            + ("s" if result.posted != 1 else ""),
            ok=True,
        )

    # Populate the twin cache with this run's high-conf events (post-POST only).
    record_twins_after_post(events, posted=result.posted)


def run_file_import(
    ctx: RunContext,
    *,
    parse,
    tag: str,
    library_mod,
    fulcra_client_cls,
    state_load,
    default_twin_policy: str = "keep",
) -> None:
    """Resolve path → parse → import. Used by the simple file-based plugins."""
    resolved = resolve_path(ctx, library_mod)
    events = list(parse(resolved))
    import_events(
        ctx, events, tag,
        fulcra_client_cls=fulcra_client_cls,
        state_load=state_load,
        default_twin_policy=default_twin_policy,
    )


def rss_since(ctx: RunContext) -> datetime | None:
    """Parse ctx.state.watermark into a tz-aware datetime, or None for full backfill.

    RSS feeds are append-only and ordered, so a plain >= comparison is
    sufficient — no rewind needed (unlike Last.fm's 1-hour rewind).
    """
    if not ctx.state.watermark:
        return None
    return datetime.fromisoformat(
        ctx.state.watermark.replace("Z", "+00:00")
    )


def rss_import_and_advance(
    ctx: RunContext,
    events: list,
    *,
    tag: str,
    since: datetime | None,
    max_entries: int | None,
    fulcra_client_cls,
    state_load,
    newest_iso=newest_event_iso,
) -> None:
    """Filter events by watermark, optionally cap, import, and advance watermark.

    Shared tail common to all three RSS plugins (generic-rss, letterboxd,
    goodreads):
      1. Filter to events at/after ``since`` (skip when since is None — full backfill).
      2. Sort ascending by start_time, then apply ``max_entries`` cap.
      3. ensure_tag + run_import.
      4. Advance ctx.state.watermark to the newest processed event.
    """
    if since is not None:
        events = [e for e in events if e.start_time >= since]
    # Sort oldest-first so the ``max_entries`` cap deterministically keeps the
    # oldest contiguous block. Without this, a newest-first feed would lose
    # its older middle history forever: the cap would keep the newest N, the
    # watermark would jump past everything older, and the next run would
    # filter that older history out via ``since``.
    events.sort(key=lambda e: e.start_time)
    if max_entries is not None:
        events = events[:max_entries]

    ctx.progress(stage="fetched", count=len(events))

    # Cross-source twin dedup (default "keep" → no-op); see run_scheduled_import.
    events = consult_twin_cache(ctx, events)

    media_state = state_load(STATE_PATH)
    client = fulcra_client_cls()
    client.ensure_tag(tag, media_state)
    result = client.run_import(events, media_state, claim=ctx.claim_dedup_keys,
                               unclaim=ctx.unclaim_dedup_keys)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    if result.posted > 0:
        ctx.annotation(
            f"{tag.capitalize()}: {result.posted} new annotation"
            + ("s" if result.posted != 1 else ""),
            ok=True,
        )

    # Populate the twin cache with this run's high-conf events (post-POST only).
    record_twins_after_post(events, posted=result.posted)

    # Advance even when posted == 0 — same rationale as run_scheduled_import.
    new_wm = newest_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm

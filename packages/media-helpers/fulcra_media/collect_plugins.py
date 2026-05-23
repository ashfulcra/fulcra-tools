"""fulcra-collect plugins exported by fulcra-media-helpers.

Exposes scheduled plugins (Last.fm, Deezer, Trakt, Generic RSS, Letterboxd,
Goodreads) and five manual file-based plugins: Netflix, Spotify Extended,
YouTube, Spotify IFTTT, and Apple TV takeout.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fulcra_collect.plugin import Credential, Permission, Plugin, RunContext
from fulcra_csv import ClusterPolicy, ColumnMap, apply_cluster_policy, apply_twin_decisions, find_low_conf_twins

from . import library
from . import twin_cache
from . import webhook_receiver
from .fulcra import FulcraClient
from .importers import apple_podcasts as ap
from .importers import apple_takeout as apple_takeout_importer
from .importers import deezer as deezer_importer
from .importers import generic_rss as rss_importer
from .importers import goodreads as gr_importer
from .importers import letterboxd as lb_importer
from .importers import netflix as netflix_importer
from .importers import spotify as spotify_importer
from .importers import spotify_ifttt as spotify_ifttt_importer
from .importers import trakt as trakt_importer
from .importers import youtube as youtube_importer
from .importers.generic_csv import _FP_AUTO, parse_media_csv
from .importers.lastfm import fetch_recent_tracks, normalize_history
from .state import DEFAULT_PATH as STATE_PATH
from .state import load as _state_load
from .state import save as _state_save


def newest_event_iso(events: list) -> str | None:
    """The newest start_time across `events`, as an ISO string — the new
    watermark. None when there are no events."""
    if not events:
        return None
    return max(e.start_time for e in events).isoformat()


# ---------------------------------------------------------------------------
# Last.fm scheduled plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Listened" DurationAnnotation.
# Passed to ctx.resolved_definition_id as the expected_spec so the shared
# resolver can verify an adopted definition has the right structure, or create
# a new one when none exists. Mirrors the payload produced by
# wire.duration_definition_payload (the bootstrap CLI path) — annotation_type
# and measurement_spec are the two axes that _spec_matches compares.
LASTFM_LISTENED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_lastfm(ctx: RunContext) -> None:
    api_key = ctx.credentials.get("api-key")
    if not api_key:
        raise RuntimeError("lastfm: credential 'api-key' is not set — "
                           "run `fulcra-collect set-credential lastfm api-key`")
    creds = {"api_key": api_key}

    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate — giving the same multi-machine dedup
    # guarantee that bootstrap provides without requiring bootstrap to have
    # been run on every machine.
    media_state = _state_load(STATE_PATH)
    if not media_state.listened_definition_id:
        def_id = ctx.resolved_definition_id(
            LASTFM_LISTENED_SPEC,
            canonical_name="Listened",
        )
        media_state.listened_definition_id = def_id
        _state_save(media_state)

    # Delegate to the shared helper; `since` (watermark - 1h) is computed
    # there so it stays consistent with the Deezer plugin.
    _run_scheduled_import(
        ctx,
        fetch=lambda since: fetch_recent_tracks(creds, since=since, max_pages=None),
        normalize=normalize_history,
        tag="lastfm",
    )


LASTFM_PLUGIN = Plugin(
    id="lastfm",
    name="Last.fm scrobbles",
    kind="scheduled",
    run=_run_lastfm,
    default_interval=timedelta(hours=1),
    canonical_definition_name="Listened",
    required_credentials=(
        Credential(key="api-key", label="Last.fm API key",
                   help="Create one at https://www.last.fm/api/account/create"),
    ),
)


# ---------------------------------------------------------------------------
# Shared helper: compute `since` from a watermark (1-hour rewind)
# ---------------------------------------------------------------------------

def _since_from_watermark(ctx: RunContext) -> datetime | None:
    """Return watermark - 1h as a tz-aware datetime, or None for full backfill.

    The 1-hour rewind hedges against late server-side reordering (same policy
    as Last.fm). Source-id dedup in the ingest layer discards any resulting
    duplicates.
    """
    if not ctx.state.watermark:
        return None
    return datetime.fromisoformat(
        ctx.state.watermark.replace("Z", "+00:00")
    ) - timedelta(hours=1)


def _run_scheduled_import(
    ctx: RunContext,
    *,
    fetch,
    normalize,
    tag: str,
) -> None:
    """Shared tail for simple fetch-normalize-import-advance scheduled plugins.

    Calls ``fetch(since)`` → ``normalize(raw)`` → ensure_tag + run_import →
    advances ctx.state.watermark to the newest processed event whenever the
    import completes — both posted and skipped-existing count as progress, so
    the all-duplicate steady state created by the 1-hour rewind window does
    not freeze the watermark.

    This eliminates copy-pasted watermark logic between Last.fm-shaped plugins.
    """
    since = _since_from_watermark(ctx)
    raw = list(fetch(since))
    events = list(normalize(raw))
    ctx.progress(stage="fetched", count=len(events))

    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag(tag, media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)

    # Advance even when posted == 0: every event in `events` was either posted
    # OR skipped-as-already-in-Fulcra — both count as successfully processed.
    # Gating on posted > 0 froze the watermark indefinitely in the all-duplicate
    # steady state created by the 1-hour rewind window above.
    new_wm = newest_event_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm


# ---------------------------------------------------------------------------
# Deezer listening history scheduled plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Listened" DurationAnnotation
# used by the deezer plugin.  Identical structure to LASTFM_LISTENED_SPEC and
# SPOTIFY_EXTENDED_LISTENED_SPEC — all three plugins produce "Listened" Duration
# annotations against the same shared definition.  Kept as a distinct constant
# so the resolver call below is self-documenting and so spec-shape tests are
# local to this plugin block.
DEEZER_LISTENED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_deezer(ctx: RunContext) -> None:
    """Fetch the authenticated user's Deezer play history and import it.

    Credentials: the Deezer OAuth access token must be stored in
    fulcra-collect's credential store — set it with:
        fulcra-collect set-credential deezer access-token <token>
    If the token is missing, raises RuntimeError with a clear instruction.
    """
    access_token = ctx.credentials.get("access-token")
    if not access_token:
        raise RuntimeError(
            "deezer: credential 'access-token' is not set — "
            "run `fulcra-collect set-credential deezer access-token`"
        )
    creds = {"access_token": access_token}

    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate — giving the same multi-machine dedup
    # guarantee that bootstrap provides without requiring bootstrap to have
    # been run on every machine.
    media_state = _state_load(STATE_PATH)
    if not media_state.listened_definition_id:
        def_id = ctx.resolved_definition_id(
            DEEZER_LISTENED_SPEC,
            canonical_name="Listened",
        )
        media_state.listened_definition_id = def_id
        _state_save(media_state)

    _run_scheduled_import(
        ctx,
        fetch=lambda since: deezer_importer.fetch_history(
            creds, since=since, max_pages=None
        ),
        normalize=deezer_importer.normalize_history,
        tag="deezer",
    )


DEEZER_PLUGIN = Plugin(
    id="deezer",
    name="Deezer listening history",
    kind="scheduled",
    run=_run_deezer,
    default_interval=timedelta(hours=2),
    canonical_definition_name="Listened",
    required_credentials=(
        Credential(
            key="access-token",
            label="Deezer OAuth access token",
            help=(
                "Mint an access token at https://developers.deezer.com/api/oauth "
                "or run `fulcra-media wizard deezer` for the guided flow."
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Trakt watch history scheduled plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Watched" DurationAnnotation
# used by the trakt plugin.  Same structure as NETFLIX_WATCHED_SPEC —
# all Watched plugins share the same definition.
TRAKT_WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_trakt(ctx: RunContext) -> None:
    """Fetch Trakt watch history and import it, applying cluster and twin-dedup policy.

    Authentication: Trakt credentials are read from
    ~/.config/fulcra-media/trakt.json — the user must have completed the
    Trakt device-flow wizard first (``fulcra-media wizard trakt``).  If the
    credentials file is missing, raises RuntimeError with a clear instruction.

    Interactive cluster/twin-dedup policies are NOT supported in headless mode.
    Configure them via ctx.config:
      clusters:           "drop" | "sentinel:<YYYY>" | "keep"  (default: "keep")
      twin_policy:        "auto-discard" | "keep"               (default: "keep")
      cluster_threshold:  int                                    (default: 5)

    Setting either policy to "ask" raises RuntimeError so the failure is
    obvious rather than silently skipping dedup.
    """
    clusters_spec: str = ctx.config.get("clusters", "keep")
    twin_policy: str = ctx.config.get("twin_policy", "keep")
    cluster_threshold: int = int(ctx.config.get("cluster_threshold", 5))

    if clusters_spec == "ask":
        raise RuntimeError(
            "trakt: 'ask' cluster policy is interactive — "
            "set clusters to drop, keep, or sentinel:YYYY in config"
        )
    if twin_policy == "ask":
        raise RuntimeError(
            "trakt: 'ask' twin_policy is interactive — "
            "set twin_policy to auto-discard or keep in config"
        )

    # Fetch — TraktAuth reads ~/.config/fulcra-media/trakt.json internally.
    # Surface a clear error if the file is absent or malformed.
    try:
        items = list(trakt_importer.fetch_history())
    except FileNotFoundError as exc:
        raise RuntimeError(
            "trakt: not authenticated — run `fulcra-media wizard trakt` first"
        ) from exc

    events = list(trakt_importer.normalize_history(items, cluster_threshold=cluster_threshold))
    ctx.progress(stage="fetched", count=len(events))

    # --- cluster policy ---------------------------------------------------
    # Build a ClusterPolicy from the config string and apply it to events.
    # "keep" is the do-nothing pass-through; parsing matches the CLI's
    # _resolve_cluster_policy non-interactive branches exactly.
    if clusters_spec == "keep":
        cluster_policy = ClusterPolicy(
            action="keep", cluster_size_threshold=cluster_threshold
        )
    elif clusters_spec == "drop":
        cluster_policy = ClusterPolicy(
            action="drop", cluster_size_threshold=cluster_threshold
        )
    elif clusters_spec.startswith("sentinel:"):
        try:
            year = int(clusters_spec.split(":", 1)[1])
        except ValueError as exc:
            raise RuntimeError(
                f"trakt: invalid clusters config {clusters_spec!r} — "
                "expected 'sentinel:YYYY'"
            ) from exc
        cluster_policy = ClusterPolicy(
            action="sentinel", sentinel_year=year,
            cluster_size_threshold=cluster_threshold,
        )
    else:
        raise RuntimeError(
            f"trakt: unknown clusters value {clusters_spec!r} — "
            "must be drop, keep, or sentinel:YYYY"
        )

    events = apply_cluster_policy(events, cluster_policy)

    # --- twin dedup -------------------------------------------------------
    # Mirror the non-interactive branches of cli._maybe_apply_twin_dedup.
    # "keep" → no-op.  "auto-discard" → drop any low-conf event whose
    # content_fingerprint matches a high-conf entry in the twin cache.
    if twin_policy != "keep":
        cached = twin_cache.load_for_twin_lookup()
        pairs = find_low_conf_twins(events, extra_pool=cached)
        if pairs and twin_policy == "auto-discard":
            to_drop = {twin_cache._source_id_of(low) for low, _high in pairs}
            events = apply_twin_decisions(events, to_drop)

    # --- definition resolver + import + watermark advance ----------------
    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    if not media_state.watched_definition_id:
        def_id = ctx.resolved_definition_id(
            TRAKT_WATCHED_SPEC,
            canonical_name="Watched",
        )
        media_state.watched_definition_id = def_id
        _state_save(media_state)

    client = FulcraClient()
    client.ensure_tag("trakt", media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)

    # Advance even when posted == 0 — see _run_scheduled_import for the full
    # rationale. Skipped-existing means the event is already in Fulcra; both
    # outcomes are progress the watermark must reflect.
    new_wm = newest_event_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm


TRAKT_PLUGIN = Plugin(
    id="trakt",
    name="Trakt watch history",
    kind="scheduled",
    run=_run_trakt,
    default_interval=timedelta(hours=6),
    canonical_definition_name="Watched",
    required_credentials=(),  # Auth is managed by the trakt.json creds file.
)


# ---------------------------------------------------------------------------
# Shared helpers for file-based manual plugins
# ---------------------------------------------------------------------------

def _resolve_path(ctx: RunContext) -> Path:
    """Read `path` from ctx.config, raising RuntimeError if absent."""
    raw = ctx.config.get("path")
    if not raw:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'path' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    return library.resolve(raw)


def _import_events(ctx: RunContext, events: list, tag: str) -> None:
    """Run the standard ensure_tag + run_import pipeline and report progress."""
    ctx.progress(stage="parsed", count=len(events))
    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag(tag, media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)


def _run_file_import(ctx: RunContext, *, parse, tag: str) -> None:
    """Resolve path → parse → import.  Used by the three simple file plugins."""
    resolved = _resolve_path(ctx)
    events = list(parse(resolved))
    _import_events(ctx, events, tag)


# ---------------------------------------------------------------------------
# Netflix manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Watched" DurationAnnotation
# used by the netflix plugin.  Mirrors wire.duration_definition_payload
# defaults and LASTFM_LISTENED_SPEC — same structure, different canonical name.
NETFLIX_WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_netflix(ctx: RunContext) -> None:
    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    if not media_state.watched_definition_id:
        def_id = ctx.resolved_definition_id(
            NETFLIX_WATCHED_SPEC,
            canonical_name="Watched",
        )
        media_state.watched_definition_id = def_id
        _state_save(media_state)

    _run_file_import(
        ctx,
        parse=netflix_importer.parse_auto,
        tag="netflix",
    )


NETFLIX_PLUGIN = Plugin(
    id="netflix",
    name="Netflix viewing history",
    kind="manual",
    run=_run_netflix,
    default_interval=None,
    canonical_definition_name="Watched",
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Spotify Extended Streaming History manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Listened" DurationAnnotation
# used by the spotify-extended plugin.  Identical structure to
# LASTFM_LISTENED_SPEC — both plugins produce "Listened" Duration annotations
# against the same shared definition.  Kept as a distinct constant so the
# resolver call below is self-documenting and so spec-shape tests are local to
# each plugin block.
SPOTIFY_EXTENDED_LISTENED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_spotify_extended(ctx: RunContext) -> None:
    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate.  lastfm and spotify-extended share the
    # same State.listened_definition_id field — whichever plugin runs first
    # on a new machine will populate it; the other will find it already set.
    media_state = _state_load(STATE_PATH)
    if not media_state.listened_definition_id:
        def_id = ctx.resolved_definition_id(
            SPOTIFY_EXTENDED_LISTENED_SPEC,
            canonical_name="Listened",
        )
        media_state.listened_definition_id = def_id
        _state_save(media_state)

    _run_file_import(
        ctx,
        parse=spotify_importer.parse_extended_zip,
        tag="spotify",
    )


SPOTIFY_EXTENDED_PLUGIN = Plugin(
    id="spotify-extended",
    name="Spotify Extended Streaming History",
    kind="manual",
    run=_run_spotify_extended,
    default_interval=None,
    canonical_definition_name="Listened",
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# YouTube watch history manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Watched" DurationAnnotation
# used by the youtube plugin.  Same structure as NETFLIX_WATCHED_SPEC —
# all Watched plugins share the same definition.
YOUTUBE_WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_youtube(ctx: RunContext) -> None:
    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    if not media_state.watched_definition_id:
        def_id = ctx.resolved_definition_id(
            YOUTUBE_WATCHED_SPEC,
            canonical_name="Watched",
        )
        media_state.watched_definition_id = def_id
        _state_save(media_state)

    _run_file_import(
        ctx,
        parse=youtube_importer.parse_takeout_json,
        tag="youtube",
    )


YOUTUBE_PLUGIN = Plugin(
    id="youtube",
    name="YouTube watch history",
    kind="manual",
    run=_run_youtube,
    default_interval=None,
    canonical_definition_name="Watched",
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Spotify IFTTT/GDrive backfill manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Listened" DurationAnnotation
# used by the spotify-ifttt plugin.  Identical structure to
# LASTFM_LISTENED_SPEC and SPOTIFY_EXTENDED_LISTENED_SPEC — all "Listened"
# plugins produce Duration annotations against the same shared definition.
# Kept as a distinct constant so the resolver call below is self-documenting
# and so spec-shape tests are local to this plugin block.
SPOTIFY_IFTTT_LISTENED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_spotify_ifttt(ctx: RunContext) -> None:
    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    if not media_state.listened_definition_id:
        def_id = ctx.resolved_definition_id(
            SPOTIFY_IFTTT_LISTENED_SPEC,
            canonical_name="Listened",
        )
        media_state.listened_definition_id = def_id
        _state_save(media_state)

    resolved = _resolve_path(ctx)
    tz = ZoneInfo(ctx.config.get("tz", "UTC"))
    events = list(spotify_ifttt_importer.parse_ifttt_zip(resolved, tz=tz))
    _import_events(ctx, events, "spotify")


SPOTIFY_IFTTT_PLUGIN = Plugin(
    id="spotify-ifttt",
    name="Spotify IFTTT/GDrive backfill",
    kind="manual",
    run=_run_spotify_ifttt,
    default_interval=None,
    canonical_definition_name="Listened",
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Apple TV playback (takeout) manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Watched" DurationAnnotation
# used by the apple-takeout plugin.  Same structure as NETFLIX_WATCHED_SPEC —
# all Watched plugins share the same definition.
APPLE_TAKEOUT_WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_apple_takeout(ctx: RunContext) -> None:
    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    if not media_state.watched_definition_id:
        def_id = ctx.resolved_definition_id(
            APPLE_TAKEOUT_WATCHED_SPEC,
            canonical_name="Watched",
        )
        media_state.watched_definition_id = def_id
        _state_save(media_state)

    resolved = _resolve_path(ctx)
    if resolved.is_dir():
        matches = list(resolved.rglob("Playback Activity.csv"))
        if not matches:
            raise RuntimeError(
                f"apple-takeout: no 'Playback Activity.csv' found under {resolved}"
            )
        resolved = matches[0]
    events = list(apple_takeout_importer.parse_playback_csv(resolved))
    _import_events(ctx, events, "apple-tv")


APPLE_TAKEOUT_PLUGIN = Plugin(
    id="apple-takeout",
    name="Apple TV playback (takeout)",
    kind="manual",
    run=_run_apple_takeout,
    default_interval=None,
    canonical_definition_name="Watched",
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Shared helper for RSS scheduled plugins
# ---------------------------------------------------------------------------

def _rss_since(ctx: RunContext) -> datetime | None:
    """Parse ctx.state.watermark into a tz-aware datetime, or None for full backfill.

    RSS feeds are append-only and ordered, so a plain >= comparison is
    sufficient — no rewind needed (unlike Last.fm's 1-hour rewind).
    """
    if not ctx.state.watermark:
        return None
    return datetime.fromisoformat(
        ctx.state.watermark.replace("Z", "+00:00")
    )


def _rss_import_and_advance(
    ctx: RunContext,
    events: list,
    *,
    tag: str,
    since: datetime | None,
    max_entries: int | None,
) -> None:
    """Filter events by watermark, optionally cap, import, and advance watermark.

    This is the shared tail common to all three RSS plugins:
      1. Filter to events at/after `since` (skip when since is None — full backfill).
      2. Sort ascending by start_time, then apply max_entries cap.
      3. ensure_tag + run_import.
      4. Advance ctx.state.watermark to the newest processed event.
    """
    if since is not None:
        events = [e for e in events if e.start_time >= since]
    # Sort oldest-first so the `max_entries` cap deterministically keeps the
    # oldest contiguous block. Without this, a newest-first feed would lose
    # its older middle history forever: the cap would keep the newest N, the
    # watermark would jump past everything older, and the next run would
    # filter that older history out via `since`.
    events.sort(key=lambda e: e.start_time)
    if max_entries is not None:
        events = events[:max_entries]

    ctx.progress(stage="fetched", count=len(events))
    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag(tag, media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)

    # Advance even when posted == 0 — see _run_scheduled_import for rationale.
    new_wm = newest_event_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm


# ---------------------------------------------------------------------------
# Generic RSS/Atom scheduled plugin
# ---------------------------------------------------------------------------

def _run_generic_rss(ctx: RunContext) -> None:
    feed_url = ctx.config.get("feed_url")
    if not feed_url:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'feed_url' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    service = ctx.config.get("service")
    if not service:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'service' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    category = ctx.config.get("category")
    if not category:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'category' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    max_entries: int | None = ctx.config.get("max_entries")

    since = _rss_since(ctx)
    all_events = list(rss_importer.normalize_feed(feed_url, service=service, category=category))
    _rss_import_and_advance(ctx, all_events, tag=service, since=since,
                            max_entries=max_entries)


GENERIC_RSS_PLUGIN = Plugin(
    id="generic-rss",
    name="Generic RSS/Atom feed",
    kind="scheduled",
    run=_run_generic_rss,
    default_interval=timedelta(hours=6),
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Letterboxd film diary scheduled plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Watched" DurationAnnotation
# used by the letterboxd plugin.  Same structure as NETFLIX_WATCHED_SPEC —
# all Watched plugins share the same definition.
LETTERBOXD_WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_letterboxd(ctx: RunContext) -> None:
    username = ctx.config.get("username")
    if not username:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'username' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    max_entries: int | None = ctx.config.get("max_entries")

    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    if not media_state.watched_definition_id:
        def_id = ctx.resolved_definition_id(
            LETTERBOXD_WATCHED_SPEC,
            canonical_name="Watched",
        )
        media_state.watched_definition_id = def_id
        _state_save(media_state)

    since = _rss_since(ctx)
    all_events = list(lb_importer.fetch_diary(username))
    _rss_import_and_advance(ctx, all_events, tag="letterboxd", since=since,
                            max_entries=max_entries)


LETTERBOXD_PLUGIN = Plugin(
    id="letterboxd",
    name="Letterboxd film diary",
    kind="scheduled",
    run=_run_letterboxd,
    default_interval=timedelta(hours=12),
    canonical_definition_name="Watched",
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Goodreads read shelf scheduled plugin
# ---------------------------------------------------------------------------

def _run_goodreads(ctx: RunContext) -> None:
    user_id = ctx.config.get("user_id")
    if not user_id:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'user_id' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    max_entries: int | None = ctx.config.get("max_entries")

    since = _rss_since(ctx)
    all_events = list(gr_importer.fetch_diary(user_id))
    _rss_import_and_advance(ctx, all_events, tag="goodreads", since=since,
                            max_entries=max_entries)


GOODREADS_PLUGIN = Plugin(
    id="goodreads",
    name="Goodreads read shelf",
    kind="scheduled",
    run=_run_goodreads,
    default_interval=timedelta(hours=12),
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Apple Podcasts — shared helpers
# ---------------------------------------------------------------------------

_FULL_DISK_ACCESS_PERMISSION = Permission(
    id="full-disk-access",
    explanation=(
        "Reads the on-device Apple Podcasts database (~/Library/...), which macOS "
        "guards behind Full Disk Access."
    ),
)


# ---------------------------------------------------------------------------
# Apple Podcasts (on-device) scheduled plugin
# ---------------------------------------------------------------------------

def _run_apple_podcasts(ctx: RunContext) -> None:
    """Read the local Apple Podcasts SQLite DB and import played episodes.

    Reads ctx.config["db_path"] if set, otherwise uses the default DB location
    (~/Library/Group Containers/…/MTLibrary.sqlite).  The whole DB is parsed on
    every run; source-id dedup in the ingest layer handles re-imports.  The
    watermark is advanced after a successful run so progress is visible.

    A SnapshotError (the DB is I/O-stalled, e.g. mid-iCloud-sync) is surfaced
    as a RuntimeError with a clear message so the scheduler can retry later.
    """
    raw_path = ctx.config.get("db_path")
    db_path = Path(raw_path) if raw_path else ap.DEFAULT_DB_PATH

    try:
        events = list(ap.parse_db(db_path))
    except ap.SnapshotError as exc:
        raise RuntimeError(
            f"apple-podcasts: DB snapshot failed — the database is likely "
            f"I/O-stalled (iCloud sync in progress). Try again later. "
            f"Details: {exc}"
        ) from exc

    ctx.progress(stage="parsed", count=len(events))
    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag("apple-podcasts", media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)

    # Advance even when posted == 0 — see _run_scheduled_import for rationale.
    new_wm = newest_event_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm


APPLE_PODCASTS_PLUGIN = Plugin(
    id="apple-podcasts",
    name="Apple Podcasts (on-device)",
    kind="scheduled",
    run=_run_apple_podcasts,
    default_interval=timedelta(hours=6),
    requires_network=False,
    required_permissions=(_FULL_DISK_ACCESS_PERMISSION,),
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Apple Podcasts (Time Machine recovery) manual plugin
# ---------------------------------------------------------------------------

def _run_apple_podcasts_timemachine(ctx: RunContext) -> None:
    """Walk all Time Machine snapshots and import Apple Podcasts history from each.

    This is a one-shot recovery operation — it imports every played episode
    found across every Time Machine backup.  No watermark is advanced because
    the run is manual and non-incremental; source-id dedup in the ingest layer
    prevents duplicate annotations.

    Raises RuntimeError if no Time Machine snapshots are found (volume not
    mounted).  A SnapshotError on an individual snapshot is logged and skipped
    so a single bad backup does not abort the whole recovery walk.
    """
    snapshots = ap.find_timemachine_snapshots()
    if not snapshots:
        raise RuntimeError(
            "apple-podcasts-timemachine: no Time Machine snapshots found — "
            "a Time Machine volume must be mounted"
        )

    all_events: list = []
    for snap in snapshots:
        try:
            all_events.extend(ap.parse_db(snap))
        except ap.SnapshotError as exc:
            ctx.log.warning(
                "apple-podcasts-timemachine: skipping snapshot %s — %s", snap, exc
            )

    ctx.progress(stage="parsed", count=len(all_events))
    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag("apple-podcasts", media_state)
    result = client.run_import(all_events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    # No watermark advance — this is a manual, one-shot recovery run.


APPLE_PODCASTS_TIMEMACHINE_PLUGIN = Plugin(
    id="apple-podcasts-timemachine",
    name="Apple Podcasts (Time Machine recovery)",
    kind="manual",
    run=_run_apple_podcasts_timemachine,
    default_interval=None,
    requires_network=False,
    required_permissions=(_FULL_DISK_ACCESS_PERMISSION,),
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Generic media CSV manual plugin
# ---------------------------------------------------------------------------

def _run_generic_csv(ctx: RunContext) -> None:
    """Import an arbitrary CSV (IFTTT, Pipedream, manual export) as Watched/Listened.

    All parameters are read from ctx.config.  Required keys: path, service,
    category.  Optional keys mirror the CLI flags for import generic-csv with
    the same defaults.

    Column-map keys (all optional, CLI defaults):
      ts_col        — timestamp column name (default: "timestamp")
      title_col     — title column name (default: "title")
      subtitle_col  — subtitle/artist column name (default: "artist")
      id_col        — per-content id column name (default: "id")
      duration_col  — duration-in-seconds column name (default: None)
      end_col       — explicit end_time column name (default: None)

    Other optional keys:
      tz            — IANA timezone name for naive timestamps (default: "UTC")
      confidence    — timestamp_confidence value (default: "medium")
      fingerprint   — content fingerprint kind: "auto", "none", or an explicit
                      kind string such as "music", "movie" (default: "auto")
    """
    from datetime import timezone as _timezone

    # --- Required parameters ------------------------------------------------
    path_raw = ctx.config.get("path")
    if not path_raw:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'path' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    service = ctx.config.get("service")
    if not service:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'service' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    category = ctx.config.get("category")
    if not category:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'category' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )

    # --- Optional column-map parameters (CLI defaults) ----------------------
    ts_col: str = ctx.config.get("ts_col", "timestamp")
    title_col: str = ctx.config.get("title_col", "title")
    subtitle_col: str = ctx.config.get("subtitle_col", "artist")
    id_col: str = ctx.config.get("id_col", "id")
    duration_col: str | None = ctx.config.get("duration_col", None)
    end_col: str | None = ctx.config.get("end_col", None)

    # --- Other optional parameters ------------------------------------------
    tz_name: str = ctx.config.get("tz", "UTC")
    confidence: str = ctx.config.get("confidence", "medium")
    fingerprint: str = ctx.config.get("fingerprint", "auto")

    # --- Build ColumnMap (mirror CLI's subtitle_col or None / id_col or None) -
    cm = ColumnMap(
        timestamp=ts_col,
        title=title_col,
        subtitle=subtitle_col or None,
        source_id=id_col or None,
        duration_seconds=duration_col,
        end_time=end_col,
    )

    # --- Resolve timezone (CLI shortcut: "UTC" → timezone.utc, else ZoneInfo) -
    if tz_name == "UTC":
        tz = _timezone.utc
    else:
        tz = ZoneInfo(tz_name)

    # --- Map fingerprint string → fingerprint_kind argument -----------------
    # Mirrors the CLI's two-step mapping exactly:
    #   fp_kind = None if fingerprint == "none" else (None if fingerprint == "auto" else fingerprint)
    #   fp_arg  = _FP_AUTO if fingerprint == "auto" else fp_kind
    fp_kind = None if fingerprint == "none" else (None if fingerprint == "auto" else fingerprint)
    fp_arg = _FP_AUTO if fingerprint == "auto" else fp_kind

    # --- Resolve path, parse, and import ------------------------------------
    resolved = library.resolve(path_raw)
    events = list(parse_media_csv(
        resolved,
        service=service,
        category=category,
        column_map=cm,
        tz=tz,
        confidence=confidence,
        fingerprint_kind=fp_arg,
    ))
    _import_events(ctx, events, service)


GENERIC_CSV_PLUGIN = Plugin(
    id="generic-csv",
    name="Generic media CSV",
    kind="manual",
    run=_run_generic_csv,
    default_interval=None,
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Plex/Jellyfin webhook receiver service plugin
# ---------------------------------------------------------------------------

# Loopback addresses that are safe to bind without a bearer token.
# Matches the CLI's check (cli.py: `host != "127.0.0.1" and host != "localhost"`).
# Note: the CLI does not currently include "::1" in the guard; we match it exactly.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def _run_media_webhook(ctx: RunContext) -> None:
    """Long-running Plex/Jellyfin webhook receiver.

    Binds an HTTP server on host:port (default 127.0.0.1:8765) and serves
    forever.  Refuses to start on a non-loopback host without a bearer token,
    mirroring the `fulcra-media webhook` CLI's safety check.
    """
    host: str = ctx.config.get("host", "127.0.0.1")
    port: int = int(ctx.config.get("port", 8765))
    bearer_token: str | None = ctx.credentials.get("bearer-token") or None

    # Non-loopback guard — mirrors cli.py's `webhook_serve` exactly:
    # refuse to bind a non-loopback address unless a bearer token is set.
    if host not in _LOOPBACK_HOSTS and not bearer_token:
        raise RuntimeError(
            f"media-webhook: host {host!r} is non-loopback; refusing to start "
            "without a bearer token. Set the 'bearer-token' credential "
            "(`fulcra-collect set-credential media-webhook bearer-token`) "
            "or bind on 127.0.0.1."
        )

    media_state = _state_load(STATE_PATH)
    if not media_state.watched_definition_id:
        raise RuntimeError(
            "media annotations not bootstrapped — run `fulcra-media bootstrap` first"
        )

    client = FulcraClient()
    server = webhook_receiver.make_server(
        host=host,
        port=port,
        state=media_state,
        client=client,
        bearer_token=bearer_token,
        log_stream=None,
    )
    ctx.log.info("media webhook receiver listening on %s:%s", host, port)
    server.serve_forever()


MEDIA_WEBHOOK_PLUGIN = Plugin(
    id="media-webhook",
    name="Plex/Jellyfin webhook receiver",
    kind="service",
    run=_run_media_webhook,
    required_permissions=(
        Permission(
            id="network-loopback-server",
            explanation=(
                "Runs a local HTTP server (default 127.0.0.1:8765) that "
                "Plex/Jellyfin POST playback webhooks to."
            ),
        ),
    ),
    required_credentials=(
        Credential(
            key="bearer-token",
            label="Webhook bearer token",
            help=(
                "Shared secret Plex/Jellyfin must send; required when the "
                "receiver binds a non-loopback host."
            ),
        ),
    ),
)

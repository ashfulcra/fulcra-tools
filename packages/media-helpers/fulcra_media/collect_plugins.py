"""fulcra-collect plugins exported by fulcra-media-helpers.

Exposes scheduled plugins (Last.fm, Deezer, Trakt, Generic RSS, Letterboxd,
Goodreads) and five manual file-based plugins: Netflix, Spotify Extended,
YouTube, Spotify IFTTT, and Apple TV takeout.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fulcra_collect.plugin import Credential, Plugin, RunContext
from fulcra_csv import ClusterPolicy, apply_cluster_policy, apply_twin_decisions, find_low_conf_twins

from . import library
from . import twin_cache
from .fulcra import FulcraClient
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
from .importers.lastfm import fetch_recent_tracks, normalize_history
from .state import DEFAULT_PATH as STATE_PATH
from .state import load as _state_load


def newest_event_iso(events: list) -> str | None:
    """The newest start_time across `events`, as an ISO string — the new
    watermark. None when there are no events."""
    if not events:
        return None
    return max(e.start_time for e in events).isoformat()


# ---------------------------------------------------------------------------
# Last.fm scheduled plugin
# ---------------------------------------------------------------------------

def _run_lastfm(ctx: RunContext) -> None:
    api_key = ctx.credentials.get("api-key")
    if not api_key:
        raise RuntimeError("lastfm: credential 'api-key' is not set — "
                           "run `fulcra-collect set-credential lastfm api-key`")
    creds = {"api_key": api_key}

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
    advances ctx.state.watermark when events were posted.

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

    if result.posted > 0:
        new_wm = newest_event_iso(events)
        if new_wm:
            ctx.state.watermark = new_wm


# ---------------------------------------------------------------------------
# Deezer listening history scheduled plugin
# ---------------------------------------------------------------------------

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

    # --- import + watermark advance ---------------------------------------
    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag("trakt", media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)

    if result.posted > 0:
        new_wm = newest_event_iso(events)
        if new_wm:
            ctx.state.watermark = new_wm


TRAKT_PLUGIN = Plugin(
    id="trakt",
    name="Trakt watch history",
    kind="scheduled",
    run=_run_trakt,
    default_interval=timedelta(hours=6),
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

def _run_netflix(ctx: RunContext) -> None:
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
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Spotify Extended Streaming History manual plugin
# ---------------------------------------------------------------------------

def _run_spotify_extended(ctx: RunContext) -> None:
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
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# YouTube watch history manual plugin
# ---------------------------------------------------------------------------

def _run_youtube(ctx: RunContext) -> None:
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
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Spotify IFTTT/GDrive backfill manual plugin
# ---------------------------------------------------------------------------

def _run_spotify_ifttt(ctx: RunContext) -> None:
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
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Apple TV playback (takeout) manual plugin
# ---------------------------------------------------------------------------

def _run_apple_takeout(ctx: RunContext) -> None:
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
      2. Apply max_entries cap when configured.
      3. ensure_tag + run_import.
      4. Advance ctx.state.watermark when events were posted.
    """
    if since is not None:
        events = [e for e in events if e.start_time >= since]
    if max_entries is not None:
        events = events[:max_entries]

    ctx.progress(stage="fetched", count=len(events))
    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag(tag, media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)

    if result.posted > 0:
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

def _run_letterboxd(ctx: RunContext) -> None:
    username = ctx.config.get("username")
    if not username:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'username' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    max_entries: int | None = ctx.config.get("max_entries")

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

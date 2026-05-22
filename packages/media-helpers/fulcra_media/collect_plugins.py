"""fulcra-collect plugins exported by fulcra-media-helpers.

Exposes one scheduled plugin (Last.fm) and five manual file-based plugins:
Netflix, Spotify Extended, YouTube, Spotify IFTTT, and Apple TV takeout.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fulcra_collect.plugin import Credential, Plugin, RunContext

from . import library
from .fulcra import FulcraClient
from .importers import apple_takeout as apple_takeout_importer
from .importers import netflix as netflix_importer
from .importers import spotify as spotify_importer
from .importers import spotify_ifttt as spotify_ifttt_importer
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

    # `since`: one hour before the stored watermark, to catch late
    # server-side reordering. No watermark -> full backfill.
    since: datetime | None = None
    if ctx.state.watermark:
        since = datetime.fromisoformat(
            ctx.state.watermark.replace("Z", "+00:00")
        ) - timedelta(hours=1)

    raw = list(fetch_recent_tracks(creds, since=since, max_pages=None))
    events = list(normalize_history(raw))
    ctx.progress(stage="fetched", count=len(events))

    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag("lastfm", media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)

    if result.posted > 0:
        new_wm = newest_event_iso(events)
        if new_wm:
            ctx.state.watermark = new_wm


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

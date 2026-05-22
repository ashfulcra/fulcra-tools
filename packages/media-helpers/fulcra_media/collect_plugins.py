"""fulcra-collect plugins exported by fulcra-media-helpers.

This module currently exposes the Last.fm scheduled plugin (plan 1a);
plan 1b adds the rest of the media importers here.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fulcra_collect.plugin import Credential, Plugin, RunContext

from .fulcra import FulcraClient
from .importers.lastfm import fetch_recent_tracks, normalize_history
from .state import DEFAULT_PATH as STATE_PATH
from .state import load as _state_load


def newest_event_iso(events: list) -> str | None:
    """The newest start_time across `events`, as an ISO string — the new
    watermark. None when there are no events."""
    if not events:
        return None
    return max(e.start_time for e in events).isoformat()


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

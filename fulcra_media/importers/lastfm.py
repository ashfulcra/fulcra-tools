"""Last.fm scrobble importer.

Reads PUBLIC scrobbles for a given username via the Audioscrobbler 2.0 REST
API. No OAuth — username + API key are sufficient for read-only access to
the `user.get*` family of endpoints.

Set up:
  1. Get an API key (free) at https://www.last.fm/api/account/create
  2. Save creds at ~/.config/fulcra-media/lastfm.json:
       {"username": "<user>", "api_key": "<key>"}

The importer paginates `user.getRecentTracks`, filters out the currently-playing
entry (no timestamp), and emits one NormalizedEvent per real scrobble.
Watermark on subsequent runs uses `from=<watermark - 1 hour>` to catch any
late server-side reordering of recent scrobbles.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .base import NormalizedEvent, content_fingerprint

CREDS_PATH = Path(os.path.expanduser("~/.config/fulcra-media/lastfm.json"))
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"


def load_creds() -> dict:
    """Load {username, api_key} from the canonical creds file."""
    return json.loads(CREDS_PATH.read_text())


def _extract_artist(track: dict) -> str:
    """Last.fm sometimes nests artist in an object, sometimes returns a string."""
    artist = track.get("artist")
    if isinstance(artist, dict):
        return (artist.get("#text") or artist.get("name") or "").strip()
    if isinstance(artist, str):
        return artist.strip()
    return ""


def _extract_album(track: dict) -> str:
    album = track.get("album")
    if isinstance(album, dict):
        return (album.get("#text") or "").strip()
    if isinstance(album, str):
        return album.strip()
    return ""


def _det_id(artist: str, track_name: str, uts: int) -> str:
    h = hashlib.sha256(f"{artist}|{track_name}|{uts}".encode()).hexdigest()
    return f"com.fulcra.media.lastfm.v1.{h[:16]}"


def normalize_track(track: dict) -> NormalizedEvent | None:
    """Convert one raw Last.fm track dict to a NormalizedEvent.

    Returns None for currently-playing entries (no timestamp) and for tracks
    missing the artist or name fields.
    """
    # Currently-playing: explicit attr OR (defensively) no date at all
    attrs = track.get("@attr") or {}
    if attrs.get("nowplaying") == "true":
        return None
    date = track.get("date")
    if not date or "uts" not in date:
        return None

    artist = _extract_artist(track)
    name = (track.get("name") or "").strip()
    if not artist or not name:
        return None

    uts = int(date["uts"])
    start = datetime.fromtimestamp(uts, tz=timezone.utc)
    end = start + timedelta(seconds=1)

    external: dict[str, Any] = {
        "artist": artist,
        "track": name,
        "content_fingerprint": content_fingerprint("music", artist=artist, track=name),
    }
    album = _extract_album(track)
    if album:
        external["album"] = album
    mbid = (track.get("mbid") or "").strip()
    if mbid:
        external["mbid"] = mbid
    url = (track.get("url") or "").strip()
    if url:
        external["url"] = url

    return NormalizedEvent(
        importer="lastfm",
        service="lastfm",
        category="listened",
        note=f"{artist} – {name}",
        title=name,
        start_time=start,
        end_time=end,
        deterministic_id=_det_id(artist, name, uts),
        timestamp_confidence="high",
        external_ids=external,
    )


def normalize_history(tracks: Iterable[dict]) -> Iterator[NormalizedEvent]:
    """Filter + normalize a raw track list. Drops nowplaying / malformed rows."""
    for t in tracks:
        ev = normalize_track(t)
        if ev is not None:
            yield ev


def _check_for_lastfm_error(payload: dict) -> None:
    """Last.fm returns 200 OK with {error: int, message: str} on logical errors."""
    if isinstance(payload, dict) and "error" in payload and "message" in payload:
        raise RuntimeError(f"Last.fm API error {payload['error']}: {payload['message']}")


def fetch_recent_tracks(
    creds: dict,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
    max_pages: int | None = None,
    sleep_between_pages: float = 0.25,
    transport: httpx.BaseTransport | None = None,
) -> Iterator[dict]:
    """Yield raw Last.fm scrobble dicts (newest first), paginating until done.

    creds: {"username": str, "api_key": str}
    since/until: optional Unix-timestamp window (passed as `from`/`to`)
    limit: per-page count (max 200, Last.fm's hard cap)
    max_pages: optional cap (None = walk to totalPages)
    sleep_between_pages: rate-limit hedge (Last.fm allows ~5 req/sec)
    transport: httpx transport override for tests
    """
    with httpx.Client(transport=transport, timeout=30.0,
                      follow_redirects=True) as client:
        page = 1
        while True:
            params: dict[str, str | int] = {
                "method": "user.getRecentTracks",
                "user": creds["username"],
                "api_key": creds["api_key"],
                "format": "json",
                "limit": min(limit, 200),
                "page": page,
            }
            if since is not None:
                params["from"] = int(since.timestamp())
            if until is not None:
                params["to"] = int(until.timestamp())
            r = client.get(LASTFM_BASE, params=params)
            r.raise_for_status()
            data = r.json()
            _check_for_lastfm_error(data)

            recenttracks = data.get("recenttracks") or {}
            tracks = recenttracks.get("track")
            if tracks is None:
                tracks = []
            elif isinstance(tracks, dict):
                # Single-item responses come back as a bare dict, not a list
                tracks = [tracks]
            for t in tracks:
                yield t

            attrs = recenttracks.get("@attr") or {}
            total_pages = int(attrs.get("totalPages") or 1)
            if max_pages is not None and page >= max_pages:
                return
            if page >= total_pages:
                return
            page += 1
            if sleep_between_pages > 0:
                time.sleep(sleep_between_pages)

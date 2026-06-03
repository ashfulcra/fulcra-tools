"""Deezer listening-history importer.

Reads the authenticated user's personal play history via Deezer's REST API
endpoint `GET https://api.deezer.com/user/me/history`. Unlike Spotify's
recently-played endpoint (50-track ceiling), Deezer's history is paginated
indefinitely via a `next` URL — making it the cleanest non-Last.fm direct
API path for music history.

Set up:
  1. Mint an OAuth 2.0 access token via the developer console at
     https://developers.deezer.com/api/oauth (manual for now — see
     `fulcra-media wizard deezer` for the click-through).
  2. Save creds at ~/.config/fulcra-media/deezer.json:
       {"access_token": "<token>"}

Watermark on subsequent runs: client-side timestamp filtering (Deezer's
endpoint has no `since` param), so we walk the history newest-first and
stop when items dip below the stored watermark. Source-id dedup handles
any overlap from the watermark-rewind hedge.

Rate limit: 50 req / 5 sec → sleep 100ms between page fetches.
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

from fulcra_common.cross_source_fingerprint import listened_fingerprint

from .base import NormalizedEvent, content_fingerprint

CREDS_PATH = Path(os.path.expanduser("~/.config/fulcra-media/deezer.json"))
DEEZER_HISTORY_URL = "https://api.deezer.com/user/me/history"


def load_creds() -> dict:
    """Load {access_token} from the canonical creds file."""
    return json.loads(CREDS_PATH.read_text())


def _det_id(track_id: Any, timestamp: int) -> str:
    h = hashlib.sha256(f"{track_id}|{timestamp}".encode()).hexdigest()
    return f"com.fulcra.media.deezer.v1.{h[:16]}"


def normalize_track(track: dict) -> NormalizedEvent | None:
    """Convert one raw Deezer history entry to a NormalizedEvent.

    Returns None if any required field (id, title, timestamp, artist.name)
    is missing — those rows aren't real plays we can attribute.
    """
    track_id = track.get("id")
    title = (track.get("title") or "").strip()
    ts = track.get("timestamp")
    artist_obj = track.get("artist") or {}
    artist_name = (artist_obj.get("name") or "").strip() if isinstance(artist_obj, dict) else ""

    if not track_id or not title or ts is None or not artist_name:
        return None

    try:
        uts = int(ts)
    except (TypeError, ValueError):
        return None

    start = datetime.fromtimestamp(uts, tz=timezone.utc)
    end = start + timedelta(seconds=1)

    external: dict[str, Any] = {
        "deezer_track_id": track_id,
        "artist": artist_name,
        "content_fingerprint": content_fingerprint(
            "music", artist=artist_name, track=title,
        ),
    }
    album_obj = track.get("album") or {}
    if isinstance(album_obj, dict):
        album_title = (album_obj.get("title") or "").strip()
        if album_title:
            external["album"] = album_title

    # Cross-source listen fingerprint so a Deezer play of the same track at
    # the same 5-minute bucket dedups against Last.fm / Apple Music / Spotify.
    cross = listened_fingerprint(timestamp=start, artist=artist_name, track=title)
    extra_source_ids: tuple[str, ...] = (cross,) if cross else ()

    return NormalizedEvent(
        importer="deezer",
        service="deezer",
        category="listened",
        note=f"{artist_name} – {title}",
        title=title,
        start_time=start,
        end_time=end,
        deterministic_id=_det_id(track_id, uts),
        timestamp_confidence="high",
        external_ids=external,
        extra_source_ids=extra_source_ids,
    )


def normalize_history(tracks: Iterable[dict]) -> Iterator[NormalizedEvent]:
    """Filter + normalize a raw history payload. Drops malformed rows silently."""
    for t in tracks:
        ev = normalize_track(t)
        if ev is not None:
            yield ev


def _check_deezer_error(payload: dict) -> None:
    """Deezer returns HTTP 200 with {error: {type, message, code}} on logical errors."""
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        err = payload["error"]
        raise RuntimeError(
            f"Deezer API error {err.get('code', '?')}: {err.get('message', 'unknown')}"
        )


def fetch_history(
    creds: dict,
    *,
    since: datetime | None = None,
    limit: int = 50,
    max_pages: int | None = None,
    sleep_between_pages: float = 0.1,
    transport: httpx.BaseTransport | None = None,
) -> Iterator[dict]:
    """Yield raw Deezer history dicts (newest first), paginating via `next`.

    creds: {"access_token": <token>}
    since: optional lower-bound — items with timestamp < since.timestamp() are
        skipped (Deezer's endpoint has no native time filter). When all items
        on a page are below the bar, pagination stops.
    limit: per-page count (Deezer default 25; we ask for 50 to halve round-trips)
    max_pages: optional cap (None = walk to last page)
    sleep_between_pages: rate-limit hedge (50 req/5s → 100ms safe)
    transport: httpx transport override for tests
    """
    since_ts = int(since.timestamp()) if since is not None else None

    next_url: str | None = DEEZER_HISTORY_URL
    first_params: dict[str, str | int] = {
        "access_token": creds["access_token"],
        "limit": limit,
    }

    with httpx.Client(transport=transport, timeout=30.0,
                      follow_redirects=True) as client:
        page = 0
        while next_url is not None:
            if page == 0:
                r = client.get(next_url, params=first_params)
            else:
                # `next` already carries access_token/limit/index — use as-is
                r = client.get(next_url)
            r.raise_for_status()
            data = r.json()
            _check_deezer_error(data)

            items = data.get("data") or []
            any_below_bar = False
            for t in items:
                ts = t.get("timestamp")
                if since_ts is not None and ts is not None:
                    try:
                        if int(ts) < since_ts:
                            any_below_bar = True
                            continue
                    except (TypeError, ValueError):
                        pass
                yield t

            page += 1
            if max_pages is not None and page >= max_pages:
                return
            # If the watermark cut anything on this page, the next page is
            # guaranteed older — bail early.
            if since_ts is not None and any_below_bar:
                return
            next_url = data.get("next")
            if not next_url:
                return
            if sleep_between_pages > 0:
                time.sleep(sleep_between_pages)

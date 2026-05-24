"""Spotify Extended Streaming History importer."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

from .base import NormalizedEvent, content_fingerprint

MIN_MS_PLAYED = 30000


def _det_id(ts: str, uri: str | None) -> str:
    h = hashlib.sha256(f"{ts}|{uri}".encode()).hexdigest()
    return f"com.fulcra.media.spotify-extended.v1.{h[:16]}"


def parse_extended_zip(zip_path: Path) -> Iterator[NormalizedEvent]:
    """Yield NormalizedEvents from a Spotify Extended Streaming History zip."""
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".json"):
                continue
            entries = json.loads(zf.read(name))
            for entry in entries:
                yield from _process(entry)


def _process(entry: dict) -> Iterator[NormalizedEvent]:
    ms_played = entry.get("ms_played", 0)
    if ms_played < MIN_MS_PLAYED:
        return
    if entry.get("skipped"):
        return

    ts = entry["ts"]
    end = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    start = end - timedelta(milliseconds=ms_played)

    track_uri = entry.get("spotify_track_uri")
    episode_uri = entry.get("spotify_episode_uri")

    if track_uri:
        artist = entry.get("master_metadata_album_artist_name") or ""
        track = entry.get("master_metadata_track_name") or ""
        if not artist or not track:
            return
        note = f"{artist} – {track}"
        title = track
        fp = content_fingerprint("music", artist=artist, track=track)
        det = _det_id(ts, track_uri)
        kind = "music"
    elif episode_uri:
        show = entry.get("episode_show_name") or ""
        ep_name = entry.get("episode_name") or ""
        if not show or not ep_name:
            return
        note = f"{show} – {ep_name}"
        title = show
        fp = content_fingerprint("podcast", show=show, title=ep_name)
        det = _det_id(ts, episode_uri)
        kind = "podcast"
    else:
        return

    yield NormalizedEvent(
        importer="spotify-extended",
        service="spotify",
        category="listened",
        note=note,
        title=title,
        start_time=start,
        end_time=end,
        deterministic_id=det,
        timestamp_confidence="high",
        external_ids={
            "kind": kind,
            "ms_played": ms_played,
            "platform": entry.get("platform"),
            "track_uri": track_uri,
            "episode_uri": episode_uri,
            "content_fingerprint": fp,
        },
    )

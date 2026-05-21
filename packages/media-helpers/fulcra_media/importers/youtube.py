"""YouTube watch-history importer (Google Takeout).

Google Takeout exports `Takeout/YouTube and YouTube Music/history/watch-history.json`
as a top-level JSON array of entries. Each entry has shape:

    {
      "header": "YouTube",
      "title": "Watched <video title>",
      "titleUrl": "https://www.youtube.com/watch?v=<id>",
      "subtitles": [{"name": "<channel>", "url": "<channel_url>"}],
      "time": "ISO 8601 with ms + Z",
      "products": ["YouTube"],
      "activityControls": ["YouTube watch history"]
    }

Takeout supports scheduled exports (every 2 months minimum), making it the
canonical pathway for ongoing YouTube watch capture — there's no public
"recently watched" API and the Data API explicitly disallows reading the
user's watch-history playlist.

Caveats:
- No watch duration in the export; we emit a 1s sentinel like Netflix slim.
- Entries for privacy-removed videos have a title but no titleUrl; we keep
  them with the title stripped of the "Watched " prefix.
- Non-YouTube entries can appear under different `header` values (Google
  Ads, etc.); we skip those.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .base import NormalizedEvent, content_fingerprint

_V_PARAM_RE = re.compile(r"[?&]v=([A-Za-z0-9_-]+)")
_WATCHED_PREFIX = "Watched "


def _video_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _V_PARAM_RE.search(url)
    return m.group(1) if m else None


def _det_id(video_id: str | None, url: str | None, title: str, time_iso: str) -> str:
    key = video_id or url or title
    h = hashlib.sha256(f"{key}|{time_iso}".encode()).hexdigest()
    return f"com.fulcra.media.youtube.v1.{h[:16]}"


def normalize_entry(entry: dict) -> NormalizedEvent | None:
    """Convert one Takeout entry to a NormalizedEvent.

    Returns None for non-YouTube entries, entries with no time, or entries
    with no title.
    """
    if entry.get("header") != "YouTube":
        return None
    time_str = entry.get("time")
    raw_title = entry.get("title")
    if not time_str or not raw_title:
        return None

    # Strip leading "Watched " prefix; some entries have other prefixes
    # ("Searched for…", "Liked…") that we ignore as they aren't watch events.
    if not raw_title.startswith(_WATCHED_PREFIX):
        return None
    title = raw_title[len(_WATCHED_PREFIX):].strip()
    if not title:
        return None

    start = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    # Drop sub-second precision so source-id hashing stays stable across
    # Takeout exports that may renormalize millisecond zeros differently.
    start = start.replace(microsecond=0)
    end = start + timedelta(seconds=1)

    url = entry.get("titleUrl")
    video_id = _video_id_from_url(url)
    channel = None
    channel_url = None
    subs = entry.get("subtitles") or []
    if subs and isinstance(subs, list):
        first = subs[0]
        if isinstance(first, dict):
            channel = first.get("name")
            channel_url = first.get("url")

    external: dict[str, Any] = {
        "content_fingerprint": content_fingerprint("movie", title=title),
    }
    if video_id:
        external["video_id"] = video_id
    if url:
        external["url"] = url
    if channel:
        external["channel"] = channel
    if channel_url:
        external["channel_url"] = channel_url

    return NormalizedEvent(
        importer="youtube",
        service="youtube",
        category="watched",
        note=f"YouTube: {title}" if not channel else f"{channel} – {title}",
        title=title,
        start_time=start,
        end_time=end,
        deterministic_id=_det_id(video_id, url, title, start.isoformat()),
        timestamp_confidence="high",
        external_ids=external,
    )


def parse_takeout_json(path: Path) -> Iterator[NormalizedEvent]:
    """Parse a Takeout watch-history.json and yield NormalizedEvents.

    The Takeout file is a top-level JSON array. We stream-iterate via
    json.load; this is fine for typical exports (~tens of MB). For
    multi-hundred-MB exports a streaming parser would be better, but the
    typical Takeout cadence (every 2 months) keeps files reasonable.
    """
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, list):
        raise ValueError(f"Expected top-level JSON array in {path}, got {type(raw)}")
    for entry in raw:
        ev = normalize_entry(entry)
        if ev is not None:
            yield ev

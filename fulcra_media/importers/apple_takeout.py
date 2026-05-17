"""Apple Data & Privacy takeout — Apple TV Playback Activity importer.

Schema (12 cols): Event Type, Content Type, Title, Episode Title,
Season Number, Episode Number, Start Time, End Time, Play Duration (Seconds),
Device Type, Device Model, Country.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .base import NormalizedEvent, content_fingerprint

_EXPECTED_COLS = [
    "Event Type", "Content Type", "Title", "Episode Title",
    "Season Number", "Episode Number", "Start Time", "End Time",
    "Play Duration (Seconds)", "Device Type", "Device Model", "Country",
]


def _det_id(start: str, title: str, ep_title: str, device_model: str) -> str:
    h = hashlib.sha256(f"{start}|{title}|{ep_title}|{device_model}".encode()).hexdigest()
    return f"com.fulcra.media.apple-takeout.v1.{h[:16]}"


def _parse_dt(value: str) -> datetime:
    """Apple's takeout dates are 'YYYY-MM-DD HH:MM:SS' — assumed UTC."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def parse_playback_csv(csv_path: Path) -> Iterator[NormalizedEvent]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != _EXPECTED_COLS:
            raise ValueError(
                f"unexpected Apple takeout header {reader.fieldnames!r}; "
                f"expected {_EXPECTED_COLS!r}"
            )
        for row in reader:
            if (row.get("Event Type") or "").strip() != "PLAY":
                continue
            content_type = (row.get("Content Type") or "").strip()
            title_field = (row.get("Title") or "").strip()
            ep_title = (row.get("Episode Title") or "").strip()
            season_str = (row.get("Season Number") or "").strip()
            episode_str = (row.get("Episode Number") or "").strip()
            start_str = row["Start Time"]
            start = _parse_dt(start_str)
            end = _parse_dt(row["End Time"])
            device_type = (row.get("Device Type") or "").strip()
            device_model = (row.get("Device Model") or "").strip()
            country = (row.get("Country") or "").strip()

            if content_type == "TV Episode" and season_str and episode_str:
                season = int(season_str)
                episode = int(episode_str)
                note = f"{title_field} S{season:02d}E{episode:02d} – {ep_title}"
                title = title_field
                fp = content_fingerprint("tv", show=title_field, season=season, episode=episode)
            else:
                # Movie (or TV row without season/episode numbers): use the bare title
                note = title_field
                title = title_field
                fp = content_fingerprint("movie", title=title_field)

            yield NormalizedEvent(
                importer="apple-takeout",
                service="apple-tv",
                category="watched",
                note=note,
                title=title,
                start_time=start,
                end_time=end,
                deterministic_id=_det_id(start_str, title_field, ep_title, device_model),
                timestamp_confidence="high",
                external_ids={
                    "device_type": device_type,
                    "device_model": device_model,
                    "country": country,
                    "content_fingerprint": fp,
                },
            )

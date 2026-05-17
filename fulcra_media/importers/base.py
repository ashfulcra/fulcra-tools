"""Shared types for importers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

VALID_CATEGORIES = {"watched", "listened"}
VALID_CONFIDENCE = {"high", "medium", "low"}


@dataclass
class NormalizedEvent:
    importer: str
    service: str
    category: str            # "watched" or "listened"
    note: str
    title: str
    start_time: datetime
    end_time: datetime
    deterministic_id: str    # full source string e.g. "com.fulcra.media.netflix.<sha16>"
    timestamp_confidence: str
    external_ids: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category not in VALID_CATEGORIES:
            raise ValueError(f"invalid category {self.category!r}")
        if self.timestamp_confidence not in VALID_CONFIDENCE:
            raise ValueError(f"invalid timestamp_confidence {self.timestamp_confidence!r}")
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("start_time and end_time must be timezone-aware")


_SLUG_KEEP_RE = re.compile(r"[^a-z0-9\- ]+")


def _slugify(value: str) -> str:
    """Lowercase, strip non-alphanumeric (except spaces and hyphens), collapse runs to hyphens."""
    s = _SLUG_KEEP_RE.sub("", (value or "").lower())
    # Treat existing hyphens as word boundaries equivalent to spaces, then collapse.
    parts = [p for p in re.split(r"[\s-]+", s) if p]
    return "-".join(parts)


def content_fingerprint(kind: str, **fields) -> str:
    """Build a stable cross-source content identifier.

    kind="tv":      requires show, season:int, episode:int
    kind="movie":   requires title; optional year
    kind="music":   requires artist, track
    kind="podcast": requires show; one of (guid, title)
    """
    if kind == "tv":
        return f"tv:{_slugify(fields['show'])}:s{fields['season']:02d}e{fields['episode']:02d}"
    if kind == "movie":
        base = f"movie:{_slugify(fields['title'])}"
        year = fields.get("year")
        return f"{base}:y{year}" if year else base
    if kind == "music":
        return f"music:{_slugify(fields['artist'])}:{_slugify(fields['track'])}"
    if kind == "podcast":
        ep = fields.get("guid") or fields.get("title")
        if ep is None:
            raise ValueError("podcast fingerprint needs guid or title")
        return f"podcast:{_slugify(fields['show'])}:{_slugify(str(ep))}"
    raise ValueError(f"unknown fingerprint kind: {kind!r}")

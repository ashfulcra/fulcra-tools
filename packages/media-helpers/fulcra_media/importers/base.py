"""Shared types for importers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from fulcra_common.ingest import DurationEvent

VALID_CATEGORIES = {"watched", "listened", "activity", "read"}
VALID_CONFIDENCE = {"high", "medium", "low"}


@dataclass
class NormalizedEvent:
    importer: str
    service: str
    category: str            # "watched" | "listened" | "activity" | "read"
    note: str
    title: str
    start_time: datetime
    end_time: datetime
    deterministic_id: str    # full source string e.g. "com.fulcra.media.netflix.<sha16>"
    timestamp_confidence: str
    external_ids: dict = field(default_factory=dict)
    # Extra source-ids appended to the Fulcra event's metadata.source array
    # ALONGSIDE deterministic_id. Used for cross-source dedup fingerprints
    # (com.fulcra.content.<kind>.v1.<hash>) so two importers that captured
    # the same listen/watch dedup against each other without losing the
    # per-plugin source_id that's how we trace "where did this come from".
    # Tuple (not list) so the dataclass stays hashable-equivalent for tests.
    extra_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.category not in VALID_CATEGORIES:
            raise ValueError(f"invalid category {self.category!r}")
        if self.timestamp_confidence not in VALID_CONFIDENCE:
            raise ValueError(f"invalid timestamp_confidence {self.timestamp_confidence!r}")
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("start_time and end_time must be timezone-aware")

    def to_duration_event(
        self, *, definition_id: str, tags: Sequence[str] = (),
    ) -> DurationEvent:
        """Produce the pipeline-side typed event from this importer-side
        intermediate. Used by FulcraClient.ingest_batch — the importer keeps
        its own NormalizedEvent shape, but the wire-construction goes
        through IngestPipeline."""
        return DurationEvent(
            definition_id=definition_id,
            source_id=self.deterministic_id,
            extra_source_ids=tuple(self.extra_source_ids),
            tags=tuple(tags),
            external_ids=dict(self.external_ids),
            note=self.note,
            title=self.title,
            service=self.service,
            timestamp_confidence=self.timestamp_confidence,
            start=self.start_time,
            end=self.end_time,
        )


_SLUG_KEEP_RE = re.compile(r"[^a-z0-9\- ]+")


def _slugify(value: str) -> str:
    """Lowercase, strip non-alphanumeric (except spaces and hyphens), collapse runs to hyphens."""
    s = _SLUG_KEEP_RE.sub("", (value or "").lower())
    # Treat existing hyphens as word boundaries equivalent to spaces, then collapse.
    parts = [p for p in re.split(r"[\s-]+", s) if p]
    return "-".join(parts)


def content_fingerprint(kind: str, **fields) -> str:
    """Build a stable cross-source content identifier.

    kind="tv":       requires show, season:int, episode:int
    kind="movie":    requires title; optional year
    kind="music":    requires artist, track
    kind="podcast":  requires show; one of (guid, title)
    kind="workout":  requires sport, athlete; optional id
    kind="book":     requires title; optional author, year
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
    if kind == "workout":
        # Workouts dedup at the (athlete, sport, exact start_time) level when
        # available; the importer is expected to surface a stable id too.
        # Caller passes id (the service's per-activity uuid/sha) when present.
        base = f"workout:{_slugify(fields['athlete'])}:{_slugify(fields['sport'])}"
        wid = fields.get("id")
        return f"{base}:{wid}" if wid else base
    if kind == "book":
        base = f"book:{_slugify(fields['title'])}"
        author = fields.get("author")
        if author:
            base += f":{_slugify(author)}"
        year = fields.get("year")
        return f"{base}:y{year}" if year else base
    raise ValueError(f"unknown fingerprint kind: {kind!r}")

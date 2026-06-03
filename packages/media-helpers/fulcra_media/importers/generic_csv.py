"""Generic media-CSV importer.

Wraps fulcra-csv's parser so any CSV (IFTTT applet output, Pipedream
workflow, manual export, hand-curated spreadsheet) can land as
Watched/Listened annotations with the same dedup + idempotency guarantees
as the dedicated importers.

The CLI surface lives in fulcra_media.cli. This module just adapts
GenericEvent → NormalizedEvent and assigns the media-specific category.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import tzinfo, timezone
from pathlib import Path

from fulcra_csv import ColumnMap, parse_csv

from fulcra_common.cross_source_fingerprint import (
    listened_fingerprint,
    watched_movie_fingerprint,
)

from .base import NormalizedEvent, content_fingerprint

VALID_CATEGORIES = {"watched", "listened", "read"}

# When a row has both subtitle (e.g. artist) and title (e.g. track), build
# a music fingerprint. Without subtitle, fall back to a "movie" fingerprint
# (which is just "<kind>:<slug>" — the consumer can still group across
# sources). Callers can override by passing fingerprint_kind explicitly.
# For "read", default to "book" — title is the book title, subtitle is the
# author (mapped to the "artist" column by default, same as for music).
_DEFAULT_FP_KIND = {
    "watched": "movie",
    "listened": "music",
    "read": "book",
}


_FP_AUTO = object()


def parse_media_csv(
    csv_path: Path,
    *,
    service: str,
    category: str,
    column_map: ColumnMap | None = None,
    tz: tzinfo = timezone.utc,
    confidence: str = "medium",
    fingerprint_kind: str | None | object = _FP_AUTO,
) -> Iterator[NormalizedEvent]:
    """Parse a CSV and yield NormalizedEvents.

    service: the service tag (e.g. "spotify", "netflix", "youtube")
    category: "watched", "listened", or "read"
    column_map: column mapping; defaults to literal column names ('timestamp',
        'title', 'artist' as subtitle, 'id' as source_id)
    tz: timezone for naive timestamps
    confidence: timestamp_confidence to attach (default "medium")
    fingerprint_kind: which content_fingerprint kind to emit. Default
        (`_FP_AUTO`) picks "music" for listened and "movie" for watched.
        Pass an explicit kind string to override, or None to skip the
        fingerprint entirely.
    """
    if category not in VALID_CATEGORIES:
        raise ValueError(f"category must be one of {VALID_CATEGORIES}, got {category!r}")
    cm = column_map or ColumnMap(
        timestamp="timestamp",
        title="title",
        subtitle="artist",
        source_id="id",
    )
    if fingerprint_kind is _FP_AUTO:
        fp_kind = _DEFAULT_FP_KIND.get(category)
    else:
        fp_kind = fingerprint_kind
    prefix = f"com.fulcra.media.generic-csv.{service}.v1"

    for ev in parse_csv(
        csv_path,
        column_map=cm,
        tz=tz,
        source_id_prefix=prefix,
        default_tag=service,
    ):
        external_ids = dict(ev.external_ids)
        # Mirror the subtitle/title onto external_ids so consumers can
        # downstream regroup by artist/show even when we lack a true ID.
        if ev.title:
            external_ids.setdefault("title", ev.title)
        subtitle_field = "artist" if fp_kind == "music" else "show"
        if ev.note and "–" in ev.note and ev.title:
            inferred_subtitle = ev.note.rsplit(" – ", 1)[0]
            if inferred_subtitle and inferred_subtitle != ev.title:
                external_ids.setdefault(subtitle_field, inferred_subtitle)

        if fp_kind == "music":
            artist = external_ids.get("artist")
            if artist and ev.title:
                external_ids["content_fingerprint"] = content_fingerprint(
                    "music", artist=artist, track=ev.title,
                )
        elif fp_kind == "movie" and ev.title:
            external_ids["content_fingerprint"] = content_fingerprint("movie", title=ev.title)
        elif fp_kind == "book" and ev.title:
            fp_kwargs: dict = {"title": ev.title}
            author = external_ids.get("artist")
            if author:
                fp_kwargs["author"] = author
            external_ids["content_fingerprint"] = content_fingerprint("book", **fp_kwargs)

        # Cross-source fingerprint: a CSV mapped to listened/watched should
        # dedup against the dedicated importers (lastfm, letterboxd, ...) the
        # same way they dedup against each other. We mirror the per-category
        # kinds: "listened" -> listened_fingerprint(artist, track), "watched"
        # -> watched_movie_fingerprint(title). The generic CSV mapping has no
        # season/episode columns, so we can only build the movie watch key
        # (a TV episode would need S/E we don't have). "read" has no
        # cross-source kind, so we emit nothing for it.
        extra_source_ids: tuple[str, ...] = ()
        cross: str | None = None
        if category == "listened":
            artist = external_ids.get("artist")
            if ev.title:
                cross = listened_fingerprint(
                    timestamp=ev.start_time,
                    artist=artist or "",
                    track=ev.title,
                )
        elif category == "watched" and ev.title:
            cross = watched_movie_fingerprint(
                timestamp=ev.start_time, title=ev.title,
            )
        if cross:
            extra_source_ids = (cross,)

        yield NormalizedEvent(
            importer="generic-csv",
            service=service,
            category=category,
            note=ev.note,
            title=ev.title or ev.note,
            start_time=ev.start_time,
            end_time=ev.end_time,
            deterministic_id=ev.source_id,
            timestamp_confidence=confidence,
            external_ids=external_ids,
            extra_source_ids=extra_source_ids,
        )

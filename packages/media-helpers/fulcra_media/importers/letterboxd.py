"""Letterboxd film-diary RSS importer.

Letterboxd's official API (api-docs.letterboxd.com) is closed beta and
explicitly won't be granted for private/personal projects. Every member's
public profile, however, publishes an RSS feed of their diary entries at
`https://letterboxd.com/<username>/rss/`. We poll that feed, mine the
service-specific `letterboxd:*` namespace fields for ratings / rewatches /
film metadata, and build movie content_fingerprints from the
filmTitle/filmYear tags so cross-source dedup against other movie
importers (Trakt, Apple TV+, ...) works naturally.

Set up:
  1. Make sure your Letterboxd diary entries are public (the default).
  2. Pick a username — that's the only credential needed.
  3. Run `fulcra-media import letterboxd --username <user>`.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from fulcra_common.cross_source_fingerprint import watched_movie_fingerprint

from . import generic_rss
from .base import NormalizedEvent, content_fingerprint

LETTERBOXD_RSS_TEMPLATE = "https://letterboxd.com/{username}/rss/"


def feed_url_for(username: str) -> str:
    """Build the public diary feed URL for `username`."""
    return LETTERBOXD_RSS_TEMPLATE.format(username=username)


def _extract_fingerprint(entry: Any) -> str | None:
    """Build a movie content_fingerprint from <letterboxd:filmTitle/filmYear>.

    feedparser lowercases namespace-prefixed tags (letterboxd:filmTitle ->
    letterboxd_filmtitle). If filmTitle is missing we can't fingerprint at
    all — fall back to None. If filmYear is missing we still build a
    title-only fingerprint (matches base.content_fingerprint's optional
    year handling).
    """
    title = (entry.get("letterboxd_filmtitle") or "").strip()
    if not title:
        return None
    year = (entry.get("letterboxd_filmyear") or "").strip() or None
    return content_fingerprint("movie", title=title, year=year)


def _extra_external_ids(entry: Any) -> dict[str, Any]:
    """Surface Letterboxd's namespace fields as external_ids."""
    out: dict[str, Any] = {}
    for key, dest in (
        ("letterboxd_filmtitle", "film_title"),
        ("letterboxd_filmyear", "film_year"),
        ("letterboxd_memberrating", "member_rating"),
        ("letterboxd_rewatch", "rewatch"),
        ("letterboxd_watcheddate", "watched_date"),
    ):
        v = entry.get(key)
        if v:
            out[dest] = v
    # tmdb:movieId is a *common* Letterboxd extension — preserve it when present
    tmdb = entry.get("tmdb_movieid")
    if tmdb:
        out["tmdb_movie_id"] = tmdb
    return out


def _extract_extra_source_ids(entry: Any, start: Any) -> tuple[str, ...]:
    """Build the cross-source movie fingerprint from Letterboxd's
    namespace tags. Letterboxd diary entries are always movies, so we
    only emit watched_movie_fingerprint."""
    title = (entry.get("letterboxd_filmtitle") or entry.get("title") or "").strip()
    if not title:
        return ()
    fp = watched_movie_fingerprint(timestamp=start, title=title)
    return (fp,) if fp else ()


def fetch_diary(
    username: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Iterator[NormalizedEvent]:
    """Yield NormalizedEvents for `username`'s public Letterboxd diary entries.

    Wraps generic_rss.normalize_feed with Letterboxd-specific service tag,
    category, importer name, and the namespace callbacks (per-importer
    content_fingerprint, extra external_ids, and the cross-source
    fingerprint that lets a Letterboxd entry dedup against the same
    movie watched via Trakt or Apple TV+).
    """
    yield from generic_rss.normalize_feed(
        feed_url_for(username),
        service="letterboxd",
        category="watched",
        importer_name="letterboxd",
        transport=transport,
        extract_fingerprint=_extract_fingerprint,
        extra_external_ids=_extra_external_ids,
        extract_extra_source_ids=_extract_extra_source_ids,
    )

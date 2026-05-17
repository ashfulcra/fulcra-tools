"""Goodreads 'books read' RSS importer.

Goodreads killed its API in December 2020 (no new keys issued; existing keys
work in degraded form). Every shelf, however, still publishes a public RSS
feed: `goodreads.com/review/list_rss/<user_id>?shelf=read`. That's the
pathway we use.

This module is a thin wrapper around the shared generic_rss machinery — it
builds the Goodreads feed URL, supplies a content_fingerprint extractor for
book metadata, and lifts the Goodreads-specific custom XML fields (which
feedparser surfaces as flat top-level keys like `user_read_at`, `book_id`,
`author_name`) into external_ids.

Set up:
  1. Find your numeric user_id from your profile URL:
     `goodreads.com/user/show/<USER_ID>-<slug>`.
  2. Verify your "read" shelf is public (default for new accounts).
  3. Run `fulcra-media import goodreads --user-id <USER_ID>`.

Timestamp pickiness:
  Goodreads' `<pubDate>` is the date the *review was added*, not the date
  the user finished the book. Goodreads also publishes a `<user_read_at>`
  field that holds the user-reported finished-reading date. When the user
  has filled in <user_read_at>, we prefer it (timestamp_confidence=high).
  When it's empty, we fall back to <pubDate> and downgrade confidence to
  medium — the date is still plausibly close, just approximate.
"""
from __future__ import annotations

import calendar
import hashlib
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import generic_rss
from .base import NormalizedEvent, content_fingerprint

GOODREADS_RSS_TEMPLATE = (
    "https://www.goodreads.com/review/list_rss/{user_id}?shelf=read"
)

# "Joe (Goodreads's review of The Hobbit)" or "Joe's review of The Hobbit"
# — both have appeared in the wild. We strip the wrapper if present.
_REVIEW_PREFIX_RE = re.compile(
    r"^.*?(?:\(Goodreads)?'s review of\s+",
    re.IGNORECASE,
)


def feed_url_for(user_id: str) -> str:
    """Build the public 'read' shelf RSS feed URL for `user_id`."""
    return GOODREADS_RSS_TEMPLATE.format(user_id=user_id)


def _strip_review_prefix(title: str) -> str:
    """Strip the "<user>'s review of " / "<user> (Goodreads's review of " prefix
    plus a trailing ")" if present.

    Returns the input unchanged when no prefix matches — typical Goodreads
    RSS uses the bare book title in <title>.
    """
    if not title:
        return ""
    stripped = _REVIEW_PREFIX_RE.sub("", title)
    if stripped != title and stripped.endswith(")"):
        stripped = stripped[:-1]
    return stripped.strip()


def _parse_rss_datetime(raw: str) -> datetime | None:
    """Parse an RFC 822 / ISO 8601 datetime string into a tz-aware UTC datetime.

    Used for <user_read_at>, which feedparser exposes as a raw string rather
    than a pre-parsed struct_time. Returns None on parse failure or empty.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Try RFC 822 first (Goodreads' usual format).
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    # ISO 8601 fallback.
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _shelves_list(raw: Any) -> list[str]:
    """Split <user_shelves> on commas, trim each, drop empties."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    if not isinstance(raw, str):
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _int_or_none(raw: Any) -> int | None:
    """Coerce raw to int; None on empty / non-numeric."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _normalize_entry_goodreads(
    entry: Any,
    *,
    feed_meta: Any,
    feed_url: str,
    user_id: str,
) -> NormalizedEvent | None:
    """Build a NormalizedEvent for one Goodreads RSS entry.

    Goodreads' timestamp picking is special enough that we do not reuse
    generic_rss.normalize_entry verbatim — we need <user_read_at>-vs-<pubDate>
    selection and per-source confidence handling. Everything else (feed_url
    in external_ids, deterministic_id hashing, end_time sentinel) mirrors
    the generic_rss conventions so cross-feed dedup behaves predictably.
    """
    # Decide on the timestamp.
    user_read_raw = entry.get("user_read_at") or ""
    user_read_dt = _parse_rss_datetime(user_read_raw)
    if user_read_dt is not None:
        start = user_read_dt
        confidence = "high"
    else:
        # Fall back to pubDate (feedparser: published_parsed).
        st = entry.get("published_parsed")
        if st is not None:
            try:
                start = datetime.fromtimestamp(
                    calendar.timegm(st), tz=timezone.utc,
                )
            except (TypeError, ValueError, OverflowError):
                start = None
        else:
            start = _parse_rss_datetime(entry.get("published") or "")
        if start is None:
            return None
        confidence = "medium"

    # Resolve titles.
    raw_title = (entry.get("title") or "").strip()
    book_title = _strip_review_prefix(raw_title)
    if not book_title:
        return None

    author = (entry.get("author_name") or "").strip()
    link = entry.get("link") or ""
    review_guid = entry.get("id") or entry.get("guid") or link

    book_id = (entry.get("book_id") or "").strip() or None
    pub_year = _int_or_none(entry.get("book_published"))
    rating = _int_or_none(entry.get("user_rating"))
    shelves = _shelves_list(entry.get("user_shelves"))

    # deterministic_id: sha256(user_id|review_guid)[:16], namespaced.
    payload = f"{user_id}|{review_guid}".encode()
    det_hash = hashlib.sha256(payload).hexdigest()[:16]
    deterministic_id = f"com.fulcra.media.goodreads.v1.{det_hash}"

    note = f"{author} – {book_title}" if author else book_title

    external: dict[str, Any] = {
        "feed_url": feed_url,
        "feed_title": (feed_meta or {}).get("title") or feed_url,
        "review_guid": review_guid,
        "book_title": book_title,
    }
    if author:
        external["author"] = author
    if book_id:
        external["book_id"] = book_id
    if link:
        external["url"] = link
    if rating is not None and rating > 0:
        # Goodreads uses 0 to mean 'unrated'; surface only real 1-5 ratings.
        external["rating"] = rating
    if shelves:
        external["shelves"] = shelves
    if pub_year is not None:
        external["book_published_year"] = pub_year

    # Book content_fingerprint — title + author + year when available.
    fp_kwargs: dict[str, Any] = {"title": book_title}
    if author:
        fp_kwargs["author"] = author
    if pub_year is not None:
        fp_kwargs["year"] = pub_year
    external["content_fingerprint"] = content_fingerprint("book", **fp_kwargs)

    return NormalizedEvent(
        importer="goodreads",
        service="goodreads",
        category="read",
        note=note,
        title=book_title,
        start_time=start,
        end_time=start + timedelta(seconds=1),
        deterministic_id=deterministic_id,
        timestamp_confidence=confidence,
        external_ids=external,
    )


def fetch_diary(
    user_id: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Iterator[NormalizedEvent]:
    """Yield NormalizedEvents for `user_id`'s 'read' shelf entries.

    Uses generic_rss.fetch_feed for HTTP + parse, then walks entries with
    the Goodreads-specific normalizer so that <user_read_at> preference and
    confidence-downgrade behavior are honored. Skips entries with no usable
    timestamp at all (no <user_read_at> AND no <pubDate>).
    """
    feed_url = feed_url_for(user_id)
    parsed = generic_rss.fetch_feed(feed_url, transport=transport)
    feed_meta = parsed.feed or {}
    for entry in parsed.entries:
        ev = _normalize_entry_goodreads(
            entry, feed_meta=feed_meta, feed_url=feed_url, user_id=user_id,
        )
        if ev is not None:
            yield ev

"""Generic RSS 2.0 / Atom 1.0 feed importer.

Many services with closed, dying, or otherwise unfriendly APIs (Letterboxd,
Goodreads, blog/podcast feeds...) publish stable public RSS/Atom feeds of
user activity. This module is the shared parser those importers ride on:
fetch the feed, normalize each entry to a NormalizedEvent, deduplicate by
deterministic_id keyed off (feed URL, entry id, published time).

Two flavors are handled by a single code path because feedparser already
normalizes RSS 2.0 and Atom 1.0 into the same FeedParserDict shape — we
just read the common fields.

The module is "generic" in two ways:
  - the timestamp / id / title fields it pulls are the universal ones
    (`published`, `id`, `link`, `title`, `summary`)
  - per-service extras (custom XML namespaces, content fingerprints) flow
    in via the `extract_fingerprint` and `extra_external_ids` callbacks

Consumers (letterboxd, goodreads, ...) wrap normalize_feed with their own
defaults — service name, category, importer_name, and the callback that
mines their namespace-specific fields.
"""
from __future__ import annotations

import calendar
import hashlib
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import httpx

from .base import NormalizedEvent


def fetch_feed(
    feed_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> feedparser.FeedParserDict:
    """GET the feed and return the parsed feedparser dict.

    Uses httpx so tests can swap in a MockTransport. feedparser parses
    bytes directly; we don't need its built-in HTTP support.

    Raises:
        httpx.HTTPStatusError on non-2xx responses.
    """
    with httpx.Client(transport=transport, timeout=timeout,
                      follow_redirects=True) as client:
        r = client.get(feed_url)
        r.raise_for_status()
        return feedparser.parse(r.content)


def _entry_datetime(entry: Any) -> datetime | None:
    """Pull a tz-aware UTC datetime from an entry, preferring published over updated.

    feedparser exposes pre-parsed time.struct_time values as
    `published_parsed` / `updated_parsed` (always UTC after RFC822/ISO8601
    normalization). If those are absent but a string field is present, fall
    back to parsing the string. Returns None when nothing usable is available.
    """
    for parsed_key, raw_key in (("published_parsed", "published"),
                                ("updated_parsed", "updated")):
        st = entry.get(parsed_key)
        if st is not None:
            try:
                return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass
        raw = entry.get(raw_key)
        if raw:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _entry_link(entry: Any) -> str:
    """Resolve the entry's canonical URL — Atom puts it in links[], RSS in link."""
    link = entry.get("link")
    if isinstance(link, str) and link:
        return link
    links = entry.get("links") or []
    for li in links:
        if isinstance(li, dict) and li.get("href"):
            return li["href"]
    return ""


def _entry_guid(entry: Any) -> str:
    """Resolve the entry's stable identifier — feedparser maps both RSS guid
    and Atom id onto `id`. Falls back to the link if neither is present."""
    gid = entry.get("id") or entry.get("guid")
    if isinstance(gid, str) and gid:
        return gid
    return _entry_link(entry)


def _det_id(importer_name: str, feed_url: str, identity: str, published_iso: str) -> str:
    """deterministic_id = sha256(feed_url | identity | published)[:16], namespaced."""
    payload = f"{feed_url}|{identity}|{published_iso}".encode()
    h = hashlib.sha256(payload).hexdigest()
    return f"com.fulcra.media.{importer_name}.v1.{h[:16]}"


def normalize_entry(
    entry: Any,
    *,
    feed_meta: Any,
    service: str,
    category: str,
    importer_name: str = "generic-rss",
    feed_url: str = "",
    extract_fingerprint: Callable[[Any], str | None] | None = None,
    extra_external_ids: Callable[[Any], dict[str, Any]] | None = None,
    extract_extra_source_ids: Callable[[Any, Any], tuple[str, ...]] | None = None,
) -> NormalizedEvent | None:
    """Convert one feedparser entry to a NormalizedEvent.

    Returns None when the entry lacks a usable timestamp — the feed-URL
    watermark relies on having one, and any "watch event" without a date
    isn't actually an event we can attribute.

    Args:
        entry: a feedparser entry dict (or anything dict-like exposing the
            same keys; this is what makes the function easy to unit-test).
        feed_meta: feed.feed from feedparser (for title context). May be
            None or empty for malformed feeds — we tolerate that.
        service: service tag to record on the event (e.g. "letterboxd").
        category: "watched" or "listened".
        importer_name: namespace component of the deterministic_id and the
            event.importer field. Defaults to "generic-rss".
        feed_url: source URL — folded into deterministic_id so the same
            entry on two different feeds gets distinct ids.
        extract_fingerprint: optional callback (entry) -> content_fingerprint
            string. Result is stored in external_ids["content_fingerprint"].
        extra_external_ids: optional callback (entry) -> dict of additional
            external_ids to merge in (e.g. Letterboxd's rewatch flag).
        extract_extra_source_ids: optional callback (entry, start_dt) ->
            tuple of cross-source fingerprint source-ids to attach. Lets a
            wrapping importer (e.g. Letterboxd) compute a category-level
            fingerprint and have it flow into the Fulcra event's source[]
            array alongside the per-importer source_id.
    """
    start = _entry_datetime(entry)
    if start is None:
        return None

    title = (entry.get("title") or "").strip()
    if not title:
        # A dateful entry with no title at all is degenerate; skip it.
        return None

    link = _entry_link(entry)
    guid = _entry_guid(entry)
    identity = guid or link or title  # last-ditch identity for det_id

    end = start + timedelta(seconds=1)
    published_iso = start.isoformat()

    external: dict[str, Any] = {
        "feed_url": feed_url,
        "feed_title": (feed_meta or {}).get("title") or feed_url or "",
        "entry_url": link,
        "guid": guid,
    }

    if extract_fingerprint is not None:
        try:
            fp = extract_fingerprint(entry)
        except Exception:
            fp = None
        if fp:
            external["content_fingerprint"] = fp

    if extra_external_ids is not None:
        try:
            extra = extra_external_ids(entry) or {}
        except Exception:
            extra = {}
        for k, v in extra.items():
            if v is not None and v != "":
                external[k] = v

    note = title
    summary = (entry.get("summary") or "").strip()
    if summary and len(summary) < 200 and summary != title:
        # Keep the note short — full HTML descriptions don't belong here.
        # Caller can stash the raw summary via extra_external_ids if needed.
        pass

    extras: tuple[str, ...] = ()
    if extract_extra_source_ids is not None:
        try:
            extras = tuple(extract_extra_source_ids(entry, start) or ())
        except Exception:
            extras = ()

    return NormalizedEvent(
        importer=importer_name,
        service=service,
        category=category,
        note=note,
        title=title,
        start_time=start,
        end_time=end,
        deterministic_id=_det_id(importer_name, feed_url, identity, published_iso),
        timestamp_confidence="high",
        external_ids=external,
        extra_source_ids=extras,
    )


def normalize_feed(
    feed_url: str,
    *,
    service: str,
    category: str,
    transport: httpx.BaseTransport | None = None,
    importer_name: str = "generic-rss",
    extract_fingerprint: Callable[[Any], str | None] | None = None,
    extra_external_ids: Callable[[Any], dict[str, Any]] | None = None,
    extract_extra_source_ids: Callable[[Any, Any], tuple[str, ...]] | None = None,
) -> Iterator[NormalizedEvent]:
    """Fetch + normalize every entry in the feed.

    Skips entries that normalize_entry rejects (missing timestamp / title).
    """
    parsed = fetch_feed(feed_url, transport=transport)
    feed_meta = parsed.feed or {}
    for entry in parsed.entries:
        ev = normalize_entry(
            entry,
            feed_meta=feed_meta,
            service=service,
            category=category,
            importer_name=importer_name,
            feed_url=feed_url,
            extract_fingerprint=extract_fingerprint,
            extra_external_ids=extra_external_ids,
            extract_extra_source_ids=extract_extra_source_ids,
        )
        if ev is not None:
            yield ev

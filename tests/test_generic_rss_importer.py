"""Tests for the generic RSS/Atom feed importer."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from fulcra_media.importers.generic_rss import (
    fetch_feed,
    normalize_entry,
    normalize_feed,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _parse(name: str):
    """Return a feedparser-parsed feed from the named fixture (bytes-fed)."""
    import feedparser
    return feedparser.parse((FIXTURES / name).read_bytes())


# ---------- normalize_entry: RSS 2.0 happy path ----------

def test_normalize_entry_rss_happy_path():
    feed = _parse("letterboxd_sample.xml")
    entry = feed.entries[0]  # Sigur Rós Live
    ev = normalize_entry(
        entry,
        feed_meta=feed.feed,
        service="letterboxd",
        category="watched",
        importer_name="letterboxd",
    )
    assert ev is not None
    assert ev.importer == "letterboxd"
    assert ev.service == "letterboxd"
    assert ev.category == "watched"
    # title and note both include the entry's title text
    assert "Sigur Rós Live" in ev.title
    assert "Sigur Rós Live" in ev.note
    # start_time is tz-aware UTC
    assert ev.start_time == datetime(2026, 5, 12, 23, 30, tzinfo=timezone.utc)
    # 1-second sentinel end
    assert (ev.end_time - ev.start_time).total_seconds() == 1
    assert ev.timestamp_confidence == "high"
    assert ev.deterministic_id.startswith("com.fulcra.media.letterboxd.v1.")
    # external_ids carries feed/entry context
    assert ev.external_ids["feed_title"] == "ash's Letterboxd diary"
    assert ev.external_ids["entry_url"] == "https://letterboxd.com/ash/film/sigur-ros-live-2026/"
    assert ev.external_ids["guid"] == "letterboxd-watch-99001"


# ---------- normalize_entry: Atom 1.0 happy path ----------

def test_normalize_entry_atom_happy_path():
    feed = _parse("generic_atom_sample.xml")
    entry = feed.entries[0]  # "An Atom Post"
    ev = normalize_entry(
        entry,
        feed_meta=feed.feed,
        service="example",
        category="watched",
    )
    assert ev is not None
    # Default importer name when not specified is "generic-rss"
    assert ev.importer == "generic-rss"
    assert ev.title == "An Atom Post"
    assert ev.start_time == datetime(2026, 5, 17, 11, 0, tzinfo=timezone.utc)
    assert ev.deterministic_id.startswith("com.fulcra.media.generic-rss.v1.")
    assert ev.external_ids["entry_url"] == "https://example.org/post/1"
    assert ev.external_ids["guid"] == "https://example.org/post/1"


# ---------- edge: missing pubDate ----------

def test_normalize_entry_missing_date_returns_none():
    feed = _parse("generic_atom_sample.xml")
    # 4th entry has neither published nor updated
    entry = feed.entries[3]
    assert entry.get("published") in (None, "")
    assert entry.get("updated") in (None, "")
    ev = normalize_entry(
        entry, feed_meta=feed.feed, service="example", category="watched",
    )
    assert ev is None


# ---------- edge: updated falls back when published missing ----------

def test_normalize_entry_uses_updated_when_published_absent():
    feed = _parse("generic_atom_sample.xml")
    entry = feed.entries[1]  # has updated but no published
    ev = normalize_entry(
        entry, feed_meta=feed.feed, service="example", category="watched",
    )
    assert ev is not None
    assert ev.start_time == datetime(2026, 5, 15, 8, 0, tzinfo=timezone.utc)


# ---------- edge: unicode round-trip ----------

def test_normalize_entry_unicode_round_trips():
    feed = _parse("generic_atom_sample.xml")
    entry = feed.entries[2]  # "Sigur Rós Live (Unicode)"
    ev = normalize_entry(
        entry, feed_meta=feed.feed, service="example", category="watched",
    )
    assert ev is not None
    assert ev.title == "Sigur Rós Live (Unicode)"
    assert "Sigur Rós" in ev.note


# ---------- determinism ----------

def test_normalize_entry_deterministic_id_stable_across_calls():
    feed = _parse("letterboxd_sample.xml")
    entry = feed.entries[0]
    a = normalize_entry(
        entry, feed_meta=feed.feed, service="letterboxd",
        category="watched", importer_name="letterboxd",
    )
    b = normalize_entry(
        entry, feed_meta=feed.feed, service="letterboxd",
        category="watched", importer_name="letterboxd",
    )
    assert a.deterministic_id == b.deterministic_id


def test_normalize_entry_deterministic_id_includes_feed_url():
    """Identical entry on different feeds should have different deterministic IDs."""
    feed = _parse("letterboxd_sample.xml")
    entry = feed.entries[0]
    a = normalize_entry(
        entry, feed_meta=feed.feed, service="letterboxd", category="watched",
        importer_name="letterboxd", feed_url="https://letterboxd.com/ash/rss/",
    )
    b = normalize_entry(
        entry, feed_meta=feed.feed, service="letterboxd", category="watched",
        importer_name="letterboxd", feed_url="https://letterboxd.com/other/rss/",
    )
    assert a.deterministic_id != b.deterministic_id


# ---------- extract_fingerprint callback ----------

def test_normalize_entry_extract_fingerprint_callback_honored():
    feed = _parse("letterboxd_sample.xml")
    entry = feed.entries[0]  # Sigur Rós Live, 2026

    def fp(e):
        from fulcra_media.importers.base import content_fingerprint
        return content_fingerprint(
            "movie",
            title=e.get("letterboxd_filmtitle"),
            year=e.get("letterboxd_filmyear"),
        )

    ev = normalize_entry(
        entry, feed_meta=feed.feed, service="letterboxd",
        category="watched", importer_name="letterboxd",
        extract_fingerprint=fp,
    )
    # NB: base._slugify strips non-ASCII (drops the 'ó'), so the slug omits it.
    assert ev.external_ids["content_fingerprint"] == "movie:sigur-rs-live:y2026"


def test_normalize_entry_extra_external_ids_callback_honored():
    feed = _parse("letterboxd_sample.xml")
    entry = feed.entries[1]  # The Fifth Element — rewatch=Yes

    def extra(e):
        out = {}
        rw = e.get("letterboxd_rewatch")
        if rw is not None:
            out["rewatch"] = rw
        return out

    ev = normalize_entry(
        entry, feed_meta=feed.feed, service="letterboxd",
        category="watched", importer_name="letterboxd",
        extra_external_ids=extra,
    )
    assert ev.external_ids["rewatch"] == "Yes"


# ---------- guid/link fallback ----------

def test_normalize_entry_uses_link_when_guid_missing(monkeypatch):
    feed = _parse("generic_atom_sample.xml")
    entry = dict(feed.entries[0])
    # Atom maps id -> guid; strip it so we fall back to link
    entry.pop("id", None)
    ev = normalize_entry(
        entry, feed_meta=feed.feed, service="example", category="watched",
    )
    assert ev is not None
    # entry_url still preserved
    assert ev.external_ids["entry_url"] == "https://example.org/post/1"


# ---------- normalize_feed iteration ----------

def test_normalize_feed_yields_for_every_dated_entry():
    """Atom fixture has 4 entries, one undated → 3 events."""
    raw = (FIXTURES / "generic_atom_sample.xml").read_bytes()

    def handler(request):
        return httpx.Response(
            200, content=raw,
            headers={"content-type": "application/atom+xml"},
        )

    events = list(normalize_feed(
        "https://example.org/atom.xml",
        service="example",
        category="watched",
        transport=httpx.MockTransport(handler),
    ))
    assert len(events) == 3


def test_normalize_feed_passes_kwargs_to_normalize_entry():
    """importer_name forwarded through normalize_feed."""
    raw = (FIXTURES / "letterboxd_sample.xml").read_bytes()
    def handler(request):
        return httpx.Response(200, content=raw)
    events = list(normalize_feed(
        "https://letterboxd.com/ash/rss/",
        service="letterboxd",
        category="watched",
        importer_name="letterboxd",
        transport=httpx.MockTransport(handler),
    ))
    assert len(events) == 4
    assert all(e.importer == "letterboxd" for e in events)


# ---------- fetch_feed transport ----------

def test_fetch_feed_uses_transport_and_returns_parsed():
    raw = (FIXTURES / "letterboxd_sample.xml").read_bytes()
    captured = []

    def handler(request):
        captured.append(str(request.url))
        return httpx.Response(200, content=raw,
                              headers={"content-type": "application/rss+xml"})

    parsed = fetch_feed(
        "https://letterboxd.com/ash/rss/",
        transport=httpx.MockTransport(handler),
    )
    assert parsed.feed.title == "ash's Letterboxd diary"
    assert len(parsed.entries) == 4
    assert captured == ["https://letterboxd.com/ash/rss/"]


def test_fetch_feed_http_error_raises():
    def handler(request):
        return httpx.Response(404, text="not found")
    with pytest.raises(httpx.HTTPStatusError):
        fetch_feed("https://example.org/missing.xml",
                   transport=httpx.MockTransport(handler))

"""Tests for the Goodreads 'read' shelf RSS consumer."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from fulcra_media.importers.goodreads import (
    GOODREADS_RSS_TEMPLATE,
    fetch_diary,
    feed_url_for,
    _strip_review_prefix,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _transport():
    raw = (FIXTURES / "goodreads_sample.xml").read_bytes()

    def handler(request):
        return httpx.Response(200, content=raw,
                              headers={"content-type": "application/rss+xml"})
    return httpx.MockTransport(handler)


def test_feed_url_for_uses_template():
    assert feed_url_for("12345") == (
        "https://www.goodreads.com/review/list_rss/12345?shelf=read"
    )
    assert "{user_id}" in GOODREADS_RSS_TEMPLATE
    assert "shelf=read" in GOODREADS_RSS_TEMPLATE


def test_fetch_diary_parses_real_shape_fixture():
    events = list(fetch_diary("12345", transport=_transport()))
    # Fixture has 3 entries — all three should normalize.
    assert len(events) == 3
    for ev in events:
        assert ev.importer == "goodreads"
        assert ev.service == "goodreads"
        assert ev.category == "read"
        assert ev.deterministic_id.startswith("com.fulcra.media.goodreads.v1.")


def test_prefers_user_read_at_over_pubdate():
    """When <user_read_at> is set, that's the actual 'finished reading' date."""
    events = list(fetch_diary("12345", transport=_transport()))
    hobbit = next(e for e in events if "Hobbit" in e.title)
    # <user_read_at>Sat, 10 May 2026 00:00:00 +0000</user_read_at>
    assert hobbit.start_time == datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
    # pubDate was Mon May 11 — we did NOT pick that.
    assert hobbit.start_time.day == 10


def test_falls_back_to_pubdate_when_user_read_at_empty():
    """Project Hail Mary has empty <user_read_at>; we use pubDate."""
    events = list(fetch_diary("12345", transport=_transport()))
    hail = next(e for e in events if "Hail Mary" in e.title)
    # pubDate Sun, 10 May 2026 12:30:00 -0700 == 2026-05-10 19:30 UTC
    assert hail.start_time == datetime(2026, 5, 10, 19, 30, tzinfo=timezone.utc)


def test_confidence_high_when_user_read_at_present():
    events = list(fetch_diary("12345", transport=_transport()))
    hobbit = next(e for e in events if "Hobbit" in e.title)
    assert hobbit.timestamp_confidence == "high"


def test_confidence_medium_when_user_read_at_absent():
    events = list(fetch_diary("12345", transport=_transport()))
    hail = next(e for e in events if "Hail Mary" in e.title)
    # Fell back to pubDate (review-add date, not finished-reading date)
    assert hail.timestamp_confidence == "medium"


def test_book_fingerprint_built_with_title_author_year():
    events = list(fetch_diary("12345", transport=_transport()))
    hobbit = next(e for e in events if "Hobbit" in e.title)
    fp = hobbit.external_ids["content_fingerprint"]
    assert fp == "book:the-hobbit:jrr-tolkien:y1937"


def test_book_fingerprint_without_year_when_missing():
    """Unknown Pleasures has no book_published — fingerprint is title+author only."""
    events = list(fetch_diary("12345", transport=_transport()))
    unknown = next(e for e in events if "Unknown Pleasures" in e.title)
    fp = unknown.external_ids["content_fingerprint"]
    assert fp == "book:unknown-pleasures:anonymous"


def test_book_title_strip_helper_handles_legacy_review_prefix():
    """Some Goodreads accounts return title as 'User's review of Book Title'.

    We strip that wrapper if present so external_ids["book_title"] is clean.
    """
    assert _strip_review_prefix("Joe's review of The Hobbit") == "The Hobbit"
    assert _strip_review_prefix(
        "Joe (Goodreads's review of The Hobbit)"
    ) == "The Hobbit"
    # No prefix → returns input unchanged
    assert _strip_review_prefix("The Hobbit") == "The Hobbit"


def test_external_ids_carry_book_metadata():
    events = list(fetch_diary("12345", transport=_transport()))
    hobbit = next(e for e in events if "Hobbit" in e.title)
    ext = hobbit.external_ids
    assert ext["book_id"] == "5907"
    assert ext["book_title"] == "The Hobbit"
    assert ext["author"] == "J.R.R. Tolkien"
    assert ext["rating"] == 5
    assert ext["shelves"] == ["fantasy", "classics"]
    assert ext["url"] == "https://www.goodreads.com/review/show/8000000001"
    assert ext["book_published_year"] == 1937


def test_rating_zero_treated_as_unrated():
    """user_rating=0 means 'unrated'; we omit it rather than store 0."""
    events = list(fetch_diary("12345", transport=_transport()))
    unknown = next(e for e in events if "Unknown Pleasures" in e.title)
    assert "rating" not in unknown.external_ids


def test_note_format_is_author_dash_title():
    events = list(fetch_diary("12345", transport=_transport()))
    hobbit = next(e for e in events if "Hobbit" in e.title)
    # Per spec: note = "author – book_title" (en-dash separator)
    assert hobbit.note == "J.R.R. Tolkien – The Hobbit"


def test_title_is_book_title():
    events = list(fetch_diary("12345", transport=_transport()))
    hobbit = next(e for e in events if "Hobbit" in e.title)
    assert hobbit.title == "The Hobbit"


def test_shelves_split_and_trimmed():
    """user_shelves is comma-separated; we split + strip."""
    events = list(fetch_diary("12345", transport=_transport()))
    hobbit = next(e for e in events if "Hobbit" in e.title)
    assert hobbit.external_ids["shelves"] == ["fantasy", "classics"]
    unknown = next(e for e in events if "Unknown Pleasures" in e.title)
    assert unknown.external_ids["shelves"] == ["currently-reading", "to-read"]


def test_fetch_diary_http_error_propagates():
    def handler(request):
        return httpx.Response(404, text="not found")
    with pytest.raises(httpx.HTTPStatusError):
        list(fetch_diary("nobody", transport=httpx.MockTransport(handler)))


def test_feed_url_in_external_ids():
    events = list(fetch_diary("12345", transport=_transport()))
    assert events[0].external_ids["feed_url"] == (
        "https://www.goodreads.com/review/list_rss/12345?shelf=read"
    )


def test_deterministic_id_stable_for_same_review():
    """Same review_guid + user_id → same deterministic_id across runs."""
    a = list(fetch_diary("12345", transport=_transport()))
    b = list(fetch_diary("12345", transport=_transport()))
    assert a[0].deterministic_id == b[0].deterministic_id


def test_deterministic_id_includes_user_id():
    """Different user_ids polling the same feed should yield distinct ids."""
    a = list(fetch_diary("12345", transport=_transport()))
    b = list(fetch_diary("99999", transport=_transport()))
    # Even though entries are identical, the feed URL differs, so ids should differ.
    assert a[0].deterministic_id != b[0].deterministic_id

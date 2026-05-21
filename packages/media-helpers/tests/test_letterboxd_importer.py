"""Tests for the Letterboxd diary RSS consumer."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from fulcra_media.importers.letterboxd import (
    LETTERBOXD_RSS_TEMPLATE,
    fetch_diary,
    feed_url_for,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _transport():
    raw = (FIXTURES / "letterboxd_sample.xml").read_bytes()

    def handler(request):
        return httpx.Response(200, content=raw,
                              headers={"content-type": "application/rss+xml"})
    return httpx.MockTransport(handler)


def test_feed_url_for_uses_template():
    assert feed_url_for("ash") == "https://letterboxd.com/ash/rss/"
    assert LETTERBOXD_RSS_TEMPLATE.endswith("/rss/")


def test_fetch_diary_parses_real_shape_fixture():
    events = list(fetch_diary("ash", transport=_transport()))
    # Fixture has 4 entries — all four should normalize.
    assert len(events) == 4
    for ev in events:
        assert ev.importer == "letterboxd"
        assert ev.service == "letterboxd"
        assert ev.category == "watched"
        assert ev.timestamp_confidence == "high"
        assert ev.deterministic_id.startswith("com.fulcra.media.letterboxd.v1.")


def test_fetch_diary_first_event_timestamp():
    events = list(fetch_diary("ash", transport=_transport()))
    # Entries should be in document order, first = Sigur Rós Live (newest).
    assert events[0].start_time == datetime(2026, 5, 12, 23, 30, tzinfo=timezone.utc)


def test_fingerprint_extraction_with_filmtitle_and_filmyear():
    events = list(fetch_diary("ash", transport=_transport()))
    # The Fifth Element, 1997 (entry index 1)
    fifth = next(e for e in events if "Fifth Element" in e.title)
    assert fifth.external_ids["content_fingerprint"] == "movie:the-fifth-element:y1997"


def test_rewatch_yes_surfaces_in_external_ids():
    events = list(fetch_diary("ash", transport=_transport()))
    fifth = next(e for e in events if "Fifth Element" in e.title)
    assert fifth.external_ids["rewatch"] == "Yes"


def test_rewatch_no_still_recorded_when_present():
    """The first entry has rewatch=No — we still surface it so the consumer
    can tell 'No' from 'absent'."""
    events = list(fetch_diary("ash", transport=_transport()))
    sigur = events[0]
    assert sigur.external_ids["rewatch"] == "No"


def test_filmtitle_and_filmyear_surfaced_as_external_ids():
    events = list(fetch_diary("ash", transport=_transport()))
    fifth = next(e for e in events if "Fifth Element" in e.title)
    assert fifth.external_ids["film_title"] == "The Fifth Element"
    assert fifth.external_ids["film_year"] == "1997"


def test_member_rating_surfaced_when_present():
    events = list(fetch_diary("ash", transport=_transport()))
    fifth = next(e for e in events if "Fifth Element" in e.title)
    assert fifth.external_ids["member_rating"] == "5.0"


def test_member_rating_omitted_when_absent():
    events = list(fetch_diary("ash", transport=_transport()))
    # "Past Lives, 2023" has no <letterboxd:memberRating>
    past = next(e for e in events if "Past Lives" in e.title)
    assert "member_rating" not in past.external_ids


def test_missing_filmyear_still_produces_fingerprint():
    """Unknown Film entry has no filmYear; fingerprint should still build (title-only)."""
    events = list(fetch_diary("ash", transport=_transport()))
    unknown = next(e for e in events if "Unknown Film" in e.title)
    # Without year, slug is just "movie:<title-slug>"
    assert unknown.external_ids["content_fingerprint"] == "movie:unknown-film"


def test_feed_url_in_external_ids():
    events = list(fetch_diary("ash", transport=_transport()))
    assert events[0].external_ids["feed_url"] == "https://letterboxd.com/ash/rss/"


def test_guid_preserved_for_letterboxd_native_id():
    events = list(fetch_diary("ash", transport=_transport()))
    assert events[0].external_ids["guid"] == "letterboxd-watch-99001"


def test_fetch_diary_http_error_propagates():
    def handler(request):
        return httpx.Response(404, text="not found")
    with pytest.raises(httpx.HTTPStatusError):
        list(fetch_diary("nobody", transport=httpx.MockTransport(handler)))

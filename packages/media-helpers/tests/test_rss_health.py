"""Tests for the shared RSS health check + the three plugin-specific
wrappers (generic-rss, letterboxd, goodreads).

The shared helper does all the fetch / parse / error-taxonomy work, so
we test it once against a representative fixture (letterboxd_sample.xml)
and then test that each wrapper:
  - returns ok=False with the right "fill in the field" message when
    its setting is missing
  - delegates to the shared helper with the URL built from the setting
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx

from fulcra_media import feed_plugin_health, rss_health


FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class _Ctx:
    """Minimal RunContext stand-in — the RSS health checks read only
    ctx.config. credentials/plugin_id are accepted for shape parity."""
    config: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    plugin_id: str = "generic-rss"


def _install_transport(monkeypatch, handler):
    """Same httpx.MockTransport injection trick as test_deezer_health."""
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(rss_health.httpx, "Client", _factory)


# ---------- rss_health_check (shared helper) ----------

def test_rss_health_check_happy_path_returns_preview(monkeypatch):
    """Valid feed bytes → ok=True with title + watched_at preview rows."""
    body = (FIXTURES / "letterboxd_sample.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body,
            headers={"content-type": "application/rss+xml"},
        )

    _install_transport(monkeypatch, handler)
    ctx = _Ctx()

    result = rss_health.rss_health_check(
        ctx,
        feed_url="https://example.com/feed.xml",
        label="Test feed",
    )

    assert result.ok is True
    assert "Test feed" in result.summary
    assert len(result.preview) >= 1
    # title is present; watched_at is ISO when feedparser could parse pubDate
    assert result.preview[0]["title"]
    assert result.preview[0]["watched_at"].startswith("20")


def test_rss_health_check_404_is_user_actionable(monkeypatch):
    """A 404 (typical for a private profile or wrong username) →
    ok=False with a check-the-username message."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"Not found")

    _install_transport(monkeypatch, handler)
    ctx = _Ctx()

    result = rss_health.rss_health_check(
        ctx,
        feed_url="https://letterboxd.com/ghost-user/rss/",
        label="Letterboxd",
    )

    assert result.ok is False
    assert "Letterboxd" in result.summary
    assert "404" in result.summary


def test_rss_health_check_empty_feed_is_ok_with_nudge(monkeypatch):
    """Feed parses cleanly but is empty → ok=True with a "no entries
    yet" nudge (the wizard still lets the user proceed)."""
    empty = b"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title></channel></rss>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=empty)

    _install_transport(monkeypatch, handler)
    ctx = _Ctx()

    result = rss_health.rss_health_check(
        ctx,
        feed_url="https://example.com/empty.xml",
        label="Test feed",
    )

    assert result.ok is True
    assert "doesn't have any entries" in result.summary


# ---------- generic_rss_health_check ----------

def test_generic_rss_no_feed_url_returns_friendly_error():
    """No feed_url → ok=False, no network call."""
    ctx = _Ctx(config={})

    result = feed_plugin_health.generic_rss_health_check(ctx)

    assert result.ok is False
    assert "feed URL" in result.summary


def test_generic_rss_delegates_to_shared_helper(monkeypatch):
    """With a feed_url set, the wrapper forwards to rss_health_check and
    propagates its HealthResult."""
    body = (FIXTURES / "letterboxd_sample.xml").read_bytes()
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=body)

    _install_transport(monkeypatch, handler)
    ctx = _Ctx(config={"feed_url": "https://example.com/my-feed.xml"})

    result = feed_plugin_health.generic_rss_health_check(ctx)

    assert result.ok is True
    assert seen_urls == ["https://example.com/my-feed.xml"]


# ---------- letterboxd_health_check ----------

def test_letterboxd_no_username_returns_friendly_error():
    ctx = _Ctx(config={})

    result = feed_plugin_health.letterboxd_health_check(ctx)

    assert result.ok is False
    assert "username" in result.summary.lower()


def test_letterboxd_url_is_built_from_username(monkeypatch):
    """The wrapper computes letterboxd.com/<username>/rss/ from the
    setting value (and tolerates a pasted profile URL via the same
    extractor the run path uses)."""
    body = (FIXTURES / "letterboxd_sample.xml").read_bytes()
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=body)

    _install_transport(monkeypatch, handler)
    # Pasted profile URL form — extractor strips it down to "ash".
    ctx = _Ctx(config={"username": "https://letterboxd.com/ash/"})

    result = feed_plugin_health.letterboxd_health_check(ctx)

    assert result.ok is True
    assert seen_urls == ["https://letterboxd.com/ash/rss/"]


# ---------- goodreads_health_check ----------

def test_goodreads_no_user_id_returns_friendly_error():
    ctx = _Ctx(config={})

    result = feed_plugin_health.goodreads_health_check(ctx)

    assert result.ok is False
    assert "user ID" in result.summary or "user id" in result.summary.lower()


def test_goodreads_url_is_built_from_user_id(monkeypatch):
    """Wrapper computes the goodreads.com/review/list_rss/<id>?shelf=read
    URL from a pasted profile URL."""
    body = (FIXTURES / "goodreads_sample.xml").read_bytes()
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=body)

    _install_transport(monkeypatch, handler)
    ctx = _Ctx(config={
        "user_id": "https://www.goodreads.com/user/show/12345678-name",
    })

    result = feed_plugin_health.goodreads_health_check(ctx)

    assert result.ok is True
    assert seen_urls == [
        "https://www.goodreads.com/review/list_rss/12345678?shelf=read",
    ]

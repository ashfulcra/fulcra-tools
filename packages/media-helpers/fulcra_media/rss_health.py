"""Shared health_check for the RSS-feed-driven plugins.

Each plugin (generic-rss, letterboxd, goodreads) computes its own feed
URL from its own settings — generic-rss passes the user's `feed_url`
through unchanged; letterboxd builds
``https://letterboxd.com/<username>/rss/`` from `username`; goodreads
builds ``https://www.goodreads.com/review/list_rss/<id>?shelf=read``
from `user_id`. Each plugin's `<service>_health_check(ctx)` does that
URL derivation (and any settings-shape validation that gives the user
a meaningful error before we hit the network) then delegates to
``rss_health_check`` here for the actual fetch + parse + preview build.

Mirrors the shape of lastfm_health.py / trakt_health.py — same return
contract (HealthResult), same defensive try/except taxonomy, same
preview-list shape ({title, subtitle, watched_at}) so the wizard's
single test_connection template renders entries from any of the three
without per-plugin code paths.
"""
from __future__ import annotations

import httpx

from fulcra_collect.plugin import HealthResult

from .importers import generic_rss as _rss_importer


def rss_health_check(
    ctx,
    *,
    feed_url: str,
    label: str,
) -> HealthResult:
    """Fetch an RSS/Atom feed, parse the first few entries, and return
    a HealthResult with a preview.

    Parameters
    ----------
    ctx : RunContext
        Accepted for signature parity with other health checks; not
        currently read by this function (callers do all the settings
        validation before delegating to us).
    feed_url : str
        The already-computed feed URL to GET. Caller is responsible for
        building this from user-provided settings.
    label : str
        Human-readable name of the service ("Goodreads", "Letterboxd",
        "the RSS feed"). Used in the summary and error messages so the
        user sees a friendly identifier rather than the raw URL.

    Returns
    -------
    HealthResult
        ok=True when the feed parses and at least one entry is present;
        ok=True with an empty preview + nudge summary when the feed
        parses but is empty (e.g. a brand-new account); ok=False on
        HTTP errors, network errors, or unparseable bodies.

    The preview entries match the shape used by lastfm_health and
    apple_podcasts_health: ``{"title": str, "subtitle": str, "watched_at": str}``.
    The wizard renders ``entry.title`` so a missing subtitle/date is
    cosmetic, not breaking.
    """
    if not feed_url:
        # Callers should have caught this and returned a more specific
        # message before delegating to us; this is a defensive backstop.
        return HealthResult(
            ok=False,
            summary=f"No {label} feed URL configured.",
        )

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            r = client.get(feed_url)
            if r.status_code == 404:
                return HealthResult(
                    ok=False,
                    summary=(
                        f"{label} returned 404. Double-check the value you "
                        f"entered on the previous step — the profile may be "
                        f"private, or the username/ID may be wrong."
                    ),
                )
            if r.status_code == 403:
                return HealthResult(
                    ok=False,
                    summary=(
                        f"{label} refused the request (HTTP 403). The "
                        f"profile may be private — make it public and "
                        f"try again."
                    ),
                )
            if 500 <= r.status_code < 600:
                return HealthResult(
                    ok=False,
                    summary=(
                        f"{label} is having a temporary issue "
                        f"(HTTP {r.status_code}). We'll retry on the next sync."
                    ),
                )
            r.raise_for_status()
            # feedparser is happy with bytes directly; the importer
            # exposes the same path via fetch_feed, but we already have
            # the response in hand so go straight to parse().
            import feedparser
            parsed = feedparser.parse(r.content)
    except httpx.TimeoutException:
        return HealthResult(
            ok=False,
            summary=f"Could not reach {label}. Check your internet.",
        )
    except httpx.HTTPError:
        return HealthResult(
            ok=False,
            summary=f"Could not reach {label}. Check your internet.",
        )
    except Exception as exc:
        return HealthResult(
            ok=False,
            summary=f"Unexpected: {type(exc).__name__}: {exc}",
        )

    entries = parsed.entries or []
    if not entries:
        # Feed parsed cleanly but is empty. Treat as ok=True with a
        # nudge — the user's setup is correct, they just don't have
        # any qualifying entries yet (e.g. fresh Letterboxd account).
        return HealthResult(
            ok=True,
            summary=(
                f"{label} feed reachable, but it doesn't have any "
                f"entries yet."
            ),
        )

    preview: list[dict] = []
    for entry in entries[:5]:
        title = (entry.get("title") or "?").strip()
        # author is RSS, dc:creator → entry.author per feedparser
        subtitle = (entry.get("author") or "").strip()
        dt = _rss_importer._entry_datetime(entry)
        watched_at = dt.isoformat() if dt else ""
        preview.append({
            "title": title,
            "subtitle": subtitle,
            "watched_at": watched_at,
        })

    return HealthResult(
        ok=True,
        summary=(
            f"{label} feed reachable. "
            f"{len(entries)} recent entr"
            f"{'y' if len(entries) == 1 else 'ies'}."
        ),
        preview=preview,
    )

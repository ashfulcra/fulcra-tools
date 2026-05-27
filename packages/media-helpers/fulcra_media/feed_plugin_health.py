"""Plugin-specific health checks for the three RSS-feed-driven plugins:
generic-rss, letterboxd, and goodreads.

Each function does the per-plugin settings validation (so the wizard's
red banner can point the user at the *specific* field they need to fix)
then delegates the actual fetch + parse + preview build to the shared
``rss_health.rss_health_check`` helper. Keeping the three thin wrappers
here keeps the per-plugin user-facing copy localised and lets all three
share the importer's URL conventions without round-tripping through the
plugin registration module (collect_plugins.py).
"""
from __future__ import annotations

from fulcra_collect.plugin import HealthResult

from .rss_health import rss_health_check


def generic_rss_health_check(ctx) -> HealthResult:
    """Verify the generic-rss plugin's configured feed.

    Reads ``feed_url`` from ctx.config; service/category are not needed
    to fetch the feed (they only affect how entries are normalised), so
    we don't gate on them here.
    """
    feed_url = (ctx.config.get("feed_url") or "").strip()
    if not feed_url:
        return HealthResult(
            ok=False,
            summary="Enter a feed URL on the previous step first.",
        )
    return rss_health_check(ctx, feed_url=feed_url, label="The RSS feed")


def letterboxd_health_check(ctx) -> HealthResult:
    """Verify the letterboxd plugin's configured username by fetching
    that user's public diary RSS feed.

    Uses the same username extraction the run path uses
    (_extract_letterboxd_username) — imported lazily to avoid a circular
    import (collect_plugins imports us; we'd otherwise import it back).
    """
    from .collect_plugins import _extract_letterboxd_username

    raw = (ctx.config.get("username") or "").strip()
    if not raw:
        return HealthResult(
            ok=False,
            summary="Enter your Letterboxd username or profile URL first.",
        )
    try:
        username = _extract_letterboxd_username(raw)
    except RuntimeError as exc:
        return HealthResult(ok=False, summary=str(exc))
    feed_url = f"https://letterboxd.com/{username}/rss/"
    return rss_health_check(ctx, feed_url=feed_url, label="Letterboxd")


def goodreads_health_check(ctx) -> HealthResult:
    """Verify the goodreads plugin's configured user_id by fetching
    that user's public 'read' shelf RSS feed.

    Uses the same user-id extractor the run path uses
    (_extract_goodreads_user_id) — lazy import for the same circular
    reason as letterboxd above.
    """
    from .collect_plugins import _extract_goodreads_user_id

    raw = (ctx.config.get("user_id") or "").strip()
    if not raw:
        return HealthResult(
            ok=False,
            summary="Enter your Goodreads user ID or profile URL first.",
        )
    try:
        user_id = _extract_goodreads_user_id(raw)
    except RuntimeError as exc:
        return HealthResult(ok=False, summary=str(exc))
    feed_url = f"https://www.goodreads.com/review/list_rss/{user_id}?shelf=read"
    return rss_health_check(ctx, feed_url=feed_url, label="Goodreads")

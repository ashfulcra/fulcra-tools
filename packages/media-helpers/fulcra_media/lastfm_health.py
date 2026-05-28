"""Last.fm health check for the fulcra-collect daemon.

Called by the daemon's /api/plugin/lastfm/health_check route to verify
that the entered API key + username are valid and Last.fm is reachable.
Returns a HealthResult with a human-readable summary and a preview of
the user's most recent scrobbles, which the wizard's test_connection
step displays to confirm data is flowing.

Mirrors the shape of trakt_health.py — same return contract, same
defensive try/except taxonomy, same preview-list shape. The wizard's
generic test_connection renderer drives both.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from fulcra_collect.plugin import HealthResult

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"


def lastfm_health_check(ctx) -> HealthResult:
    """Probe Last.fm with the stored API key + username.

    Performs one API call:
      GET user.getRecentTracks — confirms the API key is accepted and the
      username exists; the response's first page doubles as the preview.

    Returns HealthResult(ok=False) when either credential is missing,
    when Last.fm rejects the key (HTTP 403 or error.code 10), when the
    username is unknown (error.code 6), or when the network is down.
    """
    api_key = ctx.credentials.get("api-key")
    username = ctx.config.get("username")

    # The error messages here are what land in the wizard's red banner
    # when the user clicks Next on the test_connection step. Keep them
    # short and actionable — tell the user which field to go fix.
    if not api_key and not username:
        return HealthResult(
            ok=False,
            summary="Missing both API key and username. Go back and fill them in.",
        )
    if not api_key:
        return HealthResult(
            ok=False,
            summary="Missing Last.fm API key. Go back and paste it in.",
        )
    if not username:
        return HealthResult(
            ok=False,
            summary="Missing Last.fm username. Go back and enter it.",
        )

    params = {
        "method": "user.getRecentTracks",
        "user": username,
        "api_key": api_key,
        "format": "json",
        "limit": 5,
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(LASTFM_BASE, params=params)
            # Last.fm returns 200 with an inline `{"error": N, "message": ...}`
            # body for most errors (bad key, unknown user), and only emits
            # real HTTP error codes for severe failures. Check the body first.
            if r.status_code == 200:
                body = r.json()
                if isinstance(body, dict) and "error" in body:
                    code = body.get("error")
                    msg = body.get("message", "Last.fm rejected the request.")
                    if code == 6:
                        return HealthResult(
                            ok=False,
                            summary=f"Last.fm doesn't recognise the username '{username}'.",
                        )
                    if code in (10, 26):
                        return HealthResult(
                            ok=False,
                            summary="Last.fm rejected the API key. Re-check it on the previous step.",
                        )
                    return HealthResult(
                        ok=False,
                        summary=f"Last.fm: {msg}",
                    )
                tracks = (body.get("recenttracks") or {}).get("track") or []
                # Last.fm returns a single dict instead of a list when there's
                # only one track — normalise.
                if isinstance(tracks, dict):
                    tracks = [tracks]
                preview = []
                for t in tracks[:5]:
                    artist_field = t.get("artist") or {}
                    artist = (
                        artist_field.get("#text")
                        if isinstance(artist_field, dict) else str(artist_field)
                    ) or "?"
                    title = t.get("name") or "?"
                    # Last.fm's API returns date as `{uts: "<unix-seconds>"}`.
                    # The wizard's test_connection Lit component formats
                    # `watched_at` via `new Date(...).toLocaleString()`, which
                    # only parses ISO-8601 strings — feeding it the raw
                    # "1700000000" gets "Invalid Date" rendered. Convert to
                    # ISO here so every health helper's preview shape stays
                    # uniform (apple_podcasts, deezer, rss, takeout all
                    # emit isoformat()). Bail to empty string on garbage.
                    uts = (t.get("date") or {}).get("uts")
                    watched_at = ""
                    if uts:
                        try:
                            watched_at = datetime.fromtimestamp(
                                int(uts), tz=timezone.utc,
                            ).isoformat()
                        except (TypeError, ValueError, OverflowError):
                            watched_at = ""
                    preview.append({
                        "title": f"{artist} — {title}",
                        "watched_at": watched_at,
                    })
                return HealthResult(
                    ok=True,
                    summary=(
                        f"Signed in as {username}. "
                        f"{len(tracks)} recent scrobble(s)."
                    ),
                    preview=preview,
                )

            # Non-200 from Last.fm is unusual; surface the status code so a
            # developer reading the message has a starting point.
            if r.status_code == 403:
                return HealthResult(
                    ok=False,
                    summary="Last.fm rejected the API key. Re-check it on the previous step.",
                )
            if 500 <= r.status_code < 600:
                return HealthResult(
                    ok=False,
                    summary="Last.fm is having a temporary issue. We'll retry on the next sync.",
                )
            return HealthResult(
                ok=False,
                summary=f"Unexpected: Last.fm returned HTTP {r.status_code}.",
            )

    except httpx.TimeoutException:
        return HealthResult(
            ok=False,
            summary="Could not reach Last.fm. Check your internet.",
        )
    except httpx.HTTPError:
        return HealthResult(
            ok=False,
            summary="Could not reach Last.fm. Check your internet.",
        )
    except Exception as exc:
        return HealthResult(
            ok=False,
            summary=f"Unexpected: {type(exc).__name__}.",
        )

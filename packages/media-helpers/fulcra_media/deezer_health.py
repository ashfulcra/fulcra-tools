"""Deezer health check for the fulcra-collect daemon.

Mirrors lastfm_health.py / trakt_health.py — same return contract,
same defensive try/except taxonomy. The wizard's test_connection step
displays the preview list so the user can confirm their access token
is actually granting access to their listening history before the
plugin starts scheduling syncs.

Probes ``GET https://api.deezer.com/user/me/history?limit=5`` with the
configured access-token credential. Deezer returns HTTP 200 with an
inline ``{"error": {...}}`` body on most credential failures (rather
than a real HTTP error code), so we check the body shape before
trusting the status code.
"""
from __future__ import annotations

import httpx

from fulcra_collect.plugin import HealthResult

DEEZER_HISTORY_URL = "https://api.deezer.com/user/me/history"


def deezer_health_check(ctx) -> HealthResult:
    """Probe Deezer with the stored access token.

    Returns HealthResult(ok=False) when:
      - access-token credential is missing
      - Deezer returns an inline error body (typically code 300 = invalid
        token, code 200 = expired token)
      - HTTP 401/403 (token rejected at the transport layer)
      - The network is unreachable
    """
    access_token = ctx.credentials.get("access-token")
    if not access_token:
        return HealthResult(
            ok=False,
            summary="Missing Deezer access token. Go back and paste it in.",
        )

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            r = client.get(
                DEEZER_HISTORY_URL,
                params={"access_token": access_token, "limit": 5},
            )
            # Deezer's HTTP-level error path: 401/403 when the token
            # was rejected outright by the gateway.
            if r.status_code in (401, 403):
                return HealthResult(
                    ok=False,
                    summary=(
                        "Deezer rejected the access token. Re-mint a "
                        "token and paste it on the previous step."
                    ),
                )
            if 500 <= r.status_code < 600:
                return HealthResult(
                    ok=False,
                    summary=(
                        "Deezer is having a temporary issue "
                        f"(HTTP {r.status_code}). We'll retry on the next sync."
                    ),
                )
            if r.status_code != 200:
                return HealthResult(
                    ok=False,
                    summary=f"Unexpected: Deezer returned HTTP {r.status_code}.",
                )
            body = r.json()

        # Deezer's inline-error path: 200 OK with {"error": {...}} body.
        # See importers/deezer.py:_check_deezer_error for the full list;
        # codes 200/300 are the user-actionable ones (invalid / expired
        # token, missing permission).
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            err = body["error"]
            code = err.get("code")
            msg = err.get("message", "Deezer rejected the request.")
            if code in (200, 300):
                return HealthResult(
                    ok=False,
                    summary=(
                        "Deezer rejected the access token (or it lacks "
                        "the listening_history permission). Mint a fresh "
                        "token and re-paste it."
                    ),
                )
            return HealthResult(
                ok=False,
                summary=f"Deezer: {msg}",
            )

        items = body.get("data") or []
        preview = []
        for t in items[:5]:
            artist = ((t.get("artist") or {}).get("name") or "?")
            title = t.get("title") or "?"
            ts = t.get("timestamp")
            # Deezer returns timestamp as a Unix epoch int (seconds).
            watched_at = ""
            if ts is not None:
                try:
                    from datetime import datetime, timezone
                    watched_at = datetime.fromtimestamp(
                        int(ts), tz=timezone.utc
                    ).isoformat()
                except (TypeError, ValueError, OverflowError):
                    watched_at = ""
            preview.append({
                "title": f"{artist} — {title}",
                "watched_at": watched_at,
            })

        if not items:
            return HealthResult(
                ok=True,
                summary=(
                    "Signed in to Deezer, but your listening history "
                    "is empty. Play a track in Deezer then come back."
                ),
            )

        return HealthResult(
            ok=True,
            summary=(
                f"Signed in to Deezer. "
                f"{len(items)} recent track{'s' if len(items) != 1 else ''}."
            ),
            preview=preview,
        )

    except httpx.TimeoutException:
        return HealthResult(
            ok=False,
            summary="Could not reach Deezer. Check your internet.",
        )
    except httpx.HTTPError:
        return HealthResult(
            ok=False,
            summary="Could not reach Deezer. Check your internet.",
        )
    except Exception as exc:
        return HealthResult(
            ok=False,
            summary=f"Unexpected: {type(exc).__name__}.",
        )

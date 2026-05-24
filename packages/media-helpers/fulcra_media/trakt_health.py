"""Trakt health check for the fulcra-collect daemon.

Called by the daemon's /api/plugin/trakt/health_check route to verify
that the stored access token is valid and the Trakt API is reachable.
Returns a HealthResult with a human-readable summary and a preview of
the user's most recent watches, which the wizard's test_connection step
displays to confirm data is flowing.
"""
from __future__ import annotations

import httpx

from fulcra_collect.plugin import HealthResult

TRAKT_API_BASE = "https://api.trakt.tv"


def trakt_health_check(ctx) -> HealthResult:
    """Probe the Trakt API with the stored access token.

    Performs two API calls:
      1. GET /users/me — confirms the token is valid and retrieves the
         account username.
      2. GET /users/me/history?limit=5 — retrieves the 5 most recent
         watch events for the preview list.

    Returns HealthResult(ok=False) when credentials are missing, when
    the token has expired (HTTP 401), or when the Trakt API is unreachable.
    """
    from fulcra_collect import credentials as _creds

    token = _creds.get_secret(ctx.plugin_id, "access_token")
    client_id = _creds.get_secret(ctx.plugin_id, "client_id")

    if not token or not client_id:
        return HealthResult(ok=False, summary="Not signed in to Trakt yet.")

    headers = {
        "Authorization": f"Bearer {token}",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            me = client.get(f"{TRAKT_API_BASE}/users/me", headers=headers)
            if me.status_code == 401:
                return HealthResult(
                    ok=False,
                    summary="Trakt access token expired. Re-authenticate via the plugin settings.",
                )
            me.raise_for_status()
            account = me.json()

            history = client.get(
                f"{TRAKT_API_BASE}/users/me/history?limit=5",
                headers=headers,
            )
            history.raise_for_status()
            entries = history.json()

        preview = []
        for e in entries:
            if "movie" in e:
                title = e["movie"]["title"]
            elif "show" in e and "episode" in e:
                ep = e["episode"]
                title = (
                    f"{e['show']['title']} "
                    f"S{ep['season']}E{ep['number']}"
                )
            else:
                title = e.get("type", "watch")
            preview.append({"title": title, "watched_at": e.get("watched_at", "")})

        return HealthResult(
            ok=True,
            summary=(
                f"Signed in as {account.get('username', '?')}. "
                f"{len(entries)} recent watch(es)."
            ),
            preview=preview,
        )

    except httpx.HTTPStatusError as exc:
        return HealthResult(
            ok=False,
            summary=f"Trakt API error: {exc.response.status_code}",
        )
    except Exception as exc:
        return HealthResult(
            ok=False,
            summary=f"Could not reach Trakt: {exc}",
        )

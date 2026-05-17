"""Trakt history importer.

Auth: device flow handled out-of-band (see `fulcra-media auth trakt` once we
add it). This module reads creds from ~/.config/fulcra-media/trakt.json,
refreshes the access token when expired, and exposes a fetch_history()
iterator that paginates /sync/history?extended=full&limit=1000.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

CREDS_PATH = Path(os.path.expanduser("~/.config/fulcra-media/trakt.json"))
TRAKT_BASE = "https://api.trakt.tv"


def load_creds() -> dict:
    return json.loads(CREDS_PATH.read_text())


def save_creds(creds: dict) -> None:
    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDS_PATH.write_text(json.dumps(creds, indent=2, sort_keys=True))
    os.chmod(CREDS_PATH, 0o600)


class TraktAuth:
    def __init__(self) -> None:
        self._creds = load_creds()

    def _is_expired(self, slack_seconds: int = 60) -> bool:
        c = self._creds
        return (c["created_at"] + c["expires_in"] - slack_seconds) < int(time.time())

    def _refresh(self) -> None:
        c = self._creds
        r = httpx.post(
            f"{TRAKT_BASE}/oauth/token",
            json={
                "refresh_token": c["refresh_token"],
                "client_id": c["client_id"],
                "client_secret": c["client_secret"],
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Trakt token refresh failed: {r.status_code}",
                request=r.request if r._request is not None else None,  # type: ignore[arg-type]
                response=r,
            )
        tok = r.json()
        c["access_token"] = tok["access_token"]
        c["refresh_token"] = tok["refresh_token"]
        c["expires_in"] = tok["expires_in"]
        c["created_at"] = tok["created_at"]
        save_creds(c)

    def headers(self) -> dict[str, str]:
        if self._is_expired():
            self._refresh()
        c = self._creds
        return {
            "Authorization": f"Bearer {c['access_token']}",
            "trakt-api-version": "2",
            "trakt-api-key": c["client_id"],
            "Content-Type": "application/json",
        }

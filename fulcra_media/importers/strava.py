"""Strava workout importer.

Reads the authenticated athlete's activities via Strava's REST API
endpoint `GET https://www.strava.com/api/v3/athlete/activities`.
Auth is OAuth 2.0 with a 6-hour access-token lifetime; refresh tokens
are minted at app-authorization time and used to rotate the access
token (see `StravaAuth.refresh_if_needed`).

Set up:
  1. Register an API application at https://www.strava.com/settings/api
  2. Run the manual code-exchange flow (see `fulcra-media wizard strava`)
     to mint an initial access_token + refresh_token.
  3. Save creds at ~/.config/fulcra-media/strava.json:
       {
         "client_id": "...",
         "client_secret": "...",
         "access_token": "...",
         "refresh_token": "...",
         "expires_at": <unix ts when access_token expires>
       }

Watermark on subsequent runs: Strava's `after` param is a strict
"unix timestamp greater than" filter, so we pass int(watermark.timestamp())
to the endpoint. Source-id dedup catches any boundary overlap.

Rate limit: 100 req / 15 min (and 1000 req / day). We sleep 100ms
between page fetches as a hedge.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .base import NormalizedEvent, content_fingerprint

CREDS_PATH = Path(os.path.expanduser("~/.config/fulcra-media/strava.json"))
STRAVA_API_BASE = "https://www.strava.com/api/v3"
STRAVA_OAUTH_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = f"{STRAVA_API_BASE}/athlete/activities"


def load_creds() -> dict:
    """Load Strava OAuth creds from the canonical creds file."""
    return json.loads(CREDS_PATH.read_text())


def save_creds(creds: dict) -> None:
    """Persist creds to disk with 0600 perms."""
    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDS_PATH.write_text(json.dumps(creds, indent=2, sort_keys=True))
    os.chmod(CREDS_PATH, 0o600)


class StravaAuth:
    """Manages the 6-hour-lifespan Strava access token.

    Reads creds from CREDS_PATH at construction. When the cached
    `expires_at` is in the past (or within `slack_seconds` of now),
    `refresh_if_needed` calls Strava's OAuth refresh endpoint and
    rewrites the creds file in place.
    """

    def __init__(self) -> None:
        self._creds = load_creds()

    @property
    def creds(self) -> dict:
        return self._creds

    def _is_expired(self, slack_seconds: int = 60) -> bool:
        # expires_at is unix seconds; treat missing/0 as expired.
        expires_at = int(self._creds.get("expires_at") or 0)
        return (expires_at - slack_seconds) < int(time.time())

    def refresh_if_needed(self) -> None:
        if not self._is_expired():
            return
        self._refresh()

    def _refresh(self) -> None:
        c = self._creds
        r = httpx.post(
            STRAVA_OAUTH_TOKEN_URL,
            data={
                "client_id": c["client_id"],
                "client_secret": c["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": c["refresh_token"],
            },
            timeout=30,
        )
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Strava token refresh failed: {r.status_code}",
                request=r.request,
                response=r,
            )
        tok = r.json()
        c["access_token"] = tok["access_token"]
        c["refresh_token"] = tok.get("refresh_token", c["refresh_token"])
        if "expires_at" in tok:
            c["expires_at"] = tok["expires_at"]
        elif "expires_in" in tok:
            c["expires_at"] = int(time.time()) + int(tok["expires_in"])
        save_creds(c)

    def auth_header(self) -> dict[str, str]:
        self.refresh_if_needed()
        return {"Authorization": f"Bearer {self._creds['access_token']}"}


def _det_id(activity_id: Any) -> str:
    """Strava activity ids are stable 64-bit ints — use directly."""
    return f"com.fulcra.media.strava.v1.{activity_id}"


def normalize_activity(activity: dict) -> NormalizedEvent | None:
    """Convert one raw Strava activity dict to a NormalizedEvent.

    Returns None when any required field (id, athlete.id, start_date,
    elapsed_time) is missing — those rows can't be attributed correctly.
    """
    activity_id = activity.get("id")
    athlete = activity.get("athlete") or {}
    athlete_id = athlete.get("id") if isinstance(athlete, dict) else None
    start_date = activity.get("start_date")
    elapsed_time = activity.get("elapsed_time")
    if not activity_id or not athlete_id or not start_date or elapsed_time is None:
        return None

    name = (activity.get("name") or "").strip() or "Workout"
    sport_type = activity.get("sport_type") or activity.get("type") or "Workout"

    try:
        start = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = start + timedelta(seconds=int(elapsed_time))

    external: dict[str, Any] = {
        "strava_activity_id": activity_id,
        "athlete_id": athlete_id,
        "sport_type": sport_type,
        "content_fingerprint": content_fingerprint(
            "workout",
            athlete=str(athlete_id),
            sport=sport_type,
            id=str(activity_id),
        ),
    }
    distance = activity.get("distance")
    if distance is not None:
        external["distance_meters"] = distance
    moving_time = activity.get("moving_time")
    if moving_time is not None:
        external["moving_time_seconds"] = moving_time
    elevation = activity.get("total_elevation_gain")
    if elevation is not None:
        external["elevation_gain_meters"] = elevation
    hr = activity.get("average_heartrate")
    if hr is not None:
        external["average_heartrate"] = hr

    return NormalizedEvent(
        importer="strava",
        service="strava",
        category="activity",
        note=f"{sport_type} – {name}",
        title=name,
        start_time=start,
        end_time=end,
        deterministic_id=_det_id(activity_id),
        timestamp_confidence="high",
        external_ids=external,
    )


def normalize_activities(activities: Iterable[dict]) -> Iterator[NormalizedEvent]:
    """Filter + normalize a raw activities list. Drops malformed rows silently."""
    for a in activities:
        ev = normalize_activity(a)
        if ev is not None:
            yield ev


def fetch_activities(
    creds: dict,
    *,
    since: datetime | None = None,
    per_page: int = 200,
    max_pages: int | None = None,
    sleep_between_pages: float = 0.1,
    transport: httpx.BaseTransport | None = None,
) -> Iterator[dict]:
    """Yield raw Strava activity dicts, paginating until the API runs out.

    creds: {"access_token": <bearer token>, ...}. Token refresh is the
        StravaAuth class's job — callers wanting refresh should pass
        StravaAuth().creds here after calling refresh_if_needed().
    since: optional lower bound — items with start_date <= since are
        excluded server-side via the `after` (unix timestamp) param.
    per_page: per-page count (Strava max is 200).
    max_pages: optional cap (None = walk until short page).
    sleep_between_pages: rate-limit hedge (100 req / 15 min → 100ms safe).
    transport: httpx transport override for tests.

    Pagination: no `next` URL. We request page=1,2,... and stop when
    the response has fewer items than `per_page` (Strava signals "no more"
    by returning a partial or empty page).
    """
    after_ts = int(since.timestamp()) if since is not None else None

    with httpx.Client(transport=transport, timeout=30.0,
                      follow_redirects=True) as client:
        page = 1
        while True:
            params: dict[str, str | int] = {
                "per_page": per_page,
                "page": page,
            }
            if after_ts is not None:
                params["after"] = after_ts
            r = client.get(
                STRAVA_ACTIVITIES_URL,
                params=params,
                headers={"Authorization": f"Bearer {creds['access_token']}"},
            )
            r.raise_for_status()
            items = r.json()
            if not isinstance(items, list):
                # Defensive: Strava sometimes returns an error dict.
                return
            for it in items:
                yield it

            if max_pages is not None and page >= max_pages:
                return
            # No items, or partial page → no more data.
            if len(items) < per_page:
                return
            page += 1
            if sleep_between_pages > 0:
                time.sleep(sleep_between_pages)

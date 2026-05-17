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
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .base import NormalizedEvent, content_fingerprint

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


CONFIDENCE_BY_ACTION = {
    "scrobble": "high",
    "checkin": "high",
    "watch": "medium",   # often retroactive / imported
}


def detect_clusters(items: list[dict], threshold: int = 5) -> dict[str, int]:
    """Return {watched_at: count} for timestamps that have >= threshold items."""
    c = Counter(it["watched_at"] for it in items)
    return {ts: n for ts, n in c.items() if n >= threshold}


def _episode_note(show: dict, ep: dict) -> str:
    s, n, t = ep.get("season"), ep.get("number"), ep.get("title")
    base = f"{show['title']} S{s:02d}E{n:02d}"
    return f"{base} – {t}" if t else base


def _movie_note(m: dict) -> str:
    y = m.get("year")
    return f"{m['title']} ({y})" if y else m["title"]


def normalize_history(items: list[dict], cluster_threshold: int = 5) -> Iterator[NormalizedEvent]:
    """Convert raw Trakt history rows to NormalizedEvents.

    Cluster detection: any watched_at value shared by >= cluster_threshold items
    is treated as a synthetic signup/service-link timestamp. All items in such a
    cluster are flagged timestamp_confidence: low and tagged with
    timestamp_cluster_size in external_ids.
    """
    clusters = detect_clusters(items, threshold=cluster_threshold)
    for it in items:
        action = it.get("action", "watch")
        watched_at = it["watched_at"]
        confidence = CONFIDENCE_BY_ACTION.get(action, "medium")
        ext: dict = {"trakt_history_id": it["id"], "trakt_action": action}
        if watched_at in clusters:
            confidence = "low"
            ext["timestamp_cluster_size"] = clusters[watched_at]

        start = datetime.fromisoformat(watched_at.replace("Z", "+00:00"))

        if it["type"] == "episode":
            ep = it["episode"]
            show = it["show"]
            runtime_min = ep.get("runtime") or 30
            end = start + timedelta(minutes=runtime_min)
            note = _episode_note(show, ep)
            title = show["title"]
            ext["content_fingerprint"] = content_fingerprint(
                "tv", show=show["title"], season=ep["season"], episode=ep["number"]
            )
            ext["show_ids"] = show.get("ids", {})
            ext["imdb"] = ep.get("ids", {}).get("imdb") or show.get("ids", {}).get("imdb")
        elif it["type"] == "movie":
            mv = it["movie"]
            runtime_min = mv.get("runtime") or 100
            end = start + timedelta(minutes=runtime_min)
            note = _movie_note(mv)
            title = mv["title"]
            ext["content_fingerprint"] = content_fingerprint(
                "movie", title=mv["title"], year=mv.get("year")
            )
            ext["imdb"] = mv.get("ids", {}).get("imdb")
        else:
            continue  # skip unknown types

        yield NormalizedEvent(
            importer="trakt",
            service="trakt",
            category="watched",
            note=note,
            title=title,
            start_time=start,
            end_time=end,
            deterministic_id=f"com.fulcra.media.trakt.v1.history.{it['id']}",
            timestamp_confidence=confidence,
            external_ids=ext,
        )

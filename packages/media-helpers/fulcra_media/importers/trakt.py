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
from dataclasses import dataclass, field
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from fulcra_common.cross_source_fingerprint import (
    watched_movie_fingerprint,
    watched_tv_fingerprint,
)

from ..trakt_oauth import TRAKT_TOKEN_ENDPOINT
from .base import NormalizedEvent, content_fingerprint
from .poll_scan import (
    MAX_RESPONSE_BYTES, MAX_TOTAL_BYTES, MAX_REQUESTS, MAX_ROWS, SCAN_SECONDS,
    decimal, fail,
)

CREDS_PATH = Path(os.path.expanduser("~/.config/fulcra-media/trakt.json"))
TRAKT_BASE = "https://api.trakt.tv"


def load_creds() -> dict:
    from fulcra_collect.trakt_source import FileStore, _values
    return _values(FileStore(CREDS_PATH).snapshot(), file=True)


def save_creds(creds: dict) -> None:
    from fulcra_collect.trakt_source import FileStore, _values
    FileStore(CREDS_PATH).publish(_values(creds, file=True))


class TraktAuth:
    def __init__(self, *, _authority=lambda: None, _local=lambda: None) -> None:
        from fulcra_collect.trakt_source import Owner, FileStore
        self.binding = Owner(FileStore(CREDS_PATH)).capture(_authority, _local)
        self._creds = self.binding.values

    def _is_expired(self, slack_seconds: int = 60) -> bool:
        c = self._creds
        return (c["created_at"] + c["expires_in"] - slack_seconds) < int(time.time())

    def _refresh(self, *, timeout: float = 30, _remaining=None) -> None:
        def grant(c):
            r = httpx.post(TRAKT_TOKEN_ENDPOINT, json={
                "refresh_token": c["refresh_token"], "client_id": c["client_id"],
                "client_secret": c["client_secret"],
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob", "grant_type": "refresh_token",
            }, timeout=_remaining() if _remaining is not None else timeout)
            if r.status_code >= 400:
                raise RuntimeError("Trakt token refresh failed; reconnect explicitly")
            return r.json()
        self._creds = self.binding.refresh(grant, _before_claim=_remaining)

    def headers(self, *, timeout: float = 30, _remaining=None) -> dict[str, str]:
        self.binding.check_before_source()
        if self._is_expired():
            self._refresh(timeout=timeout, _remaining=_remaining)
        c = self._creds
        return {"Authorization": f"Bearer {c['access_token']}", "trakt-api-version": "2",
                "trakt-api-key": c["client_id"], "Content-Type": "application/json"}


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

        cross: str | None
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
            cross = watched_tv_fingerprint(
                timestamp=start, show=show["title"],
                season=ep["season"], episode=ep["number"],
            )
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
            cross = watched_movie_fingerprint(timestamp=start, title=mv["title"])
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
            extra_source_ids=(cross,) if cross else (),
        )


def fetch_history(
    per_page: int = 1000, *, strict_scan: bool = False, transport=None,
) -> Iterator[dict]:
    """Iterate all history items using the file-based TraktAuth credentials.

    This is the legacy path used by the old CLI wizard flow that stored
    credentials in ~/.config/fulcra-media/trakt.json. The new web-UI flow
    stores credentials in the OS keychain and uses fetch_history_with_headers
    instead. Both paths are supported during the transition period.
    """
    bounds = _ScanBounds() if strict_scan else None
    auth = TraktAuth()
    headers = auth.headers(timeout=bounds.timeout(), _remaining=bounds.timeout) if bounds else auth.headers()
    yield from fetch_history_with_headers(
        headers, per_page=per_page, strict_scan=strict_scan, transport=transport, _bounds=bounds,
        _before_request=auth.binding.check_before_source,
    )


def fetch_history_with_headers(
    headers: dict[str, str], per_page: int = 1000, *, strict_scan: bool = False,
    transport=None, _bounds: _ScanBounds | None = None, _before_request=None,
) -> Iterator[dict]:
    """Iterate all history items, paginating most-recent-first.

    Accepts explicit HTTP headers so callers can supply auth credentials
    from any source (file-based TraktAuth or keychain-based tokens).
    Used directly by _run_trakt when credentials come from the keychain.
    """
    if strict_scan:
        yield from _strict_history(headers, per_page, transport, _bounds or _ScanBounds(), _before_request)
        return
    page = 1
    with httpx.Client(timeout=60, transport=transport) as client:
        while True:
            r = client.get(
                f"{TRAKT_BASE}/sync/history",
                params={"extended": "full", "limit": per_page, "page": page},
                headers=headers,
            )
            r.raise_for_status()
            items = r.json()
            if not items:
                return
            for it in items:
                yield it
            page_count = int(r.headers.get("X-Pagination-Page-Count", "1"))
            if page >= page_count:
                return
            page += 1


@dataclass
class _ScanBounds:
    """Cumulative GET accounting across the single auth restart.

    Cooperative deadline; synchronous HTTPX timeouts are per-operation limits,
    not hard wall-clock cancellation. Grant bodies are accounted separately.
    """

    deadline: float = field(default_factory=lambda: time.monotonic() + SCAN_SECONDS)
    requests: int = 0
    bytes: int = 0
    rows: int = 0

    def charge_request(self):
        self.check()
        if self.requests >= MAX_REQUESTS:
            fail()
        self.requests += 1

    def charge_bytes(self, count):
        self.bytes += count
        if self.bytes > MAX_TOTAL_BYTES:
            fail()

    def check(self):
        if time.monotonic() >= self.deadline:
            fail()

    def timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            fail()
        return min(30.0, remaining)


def _header(response, name, maximum):
    values = response.headers.get_list(name)
    if len(values) != 1:
        fail()
    return decimal(values[0], maximum=maximum, length=10)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail()
        result[key] = value
    return result


def _read_page(client, headers, per_page, page, bounds, before_request=None):
    bounds.check()
    if bounds.requests >= MAX_REQUESTS:
        fail()
    bounds.requests += 1
    if before_request is not None:
        before_request()
    with client.stream(
        "GET", f"{TRAKT_BASE}/sync/history",
        params={"extended": "full", "limit": per_page, "page": page},
        headers={**headers, "Accept-Encoding": "identity"}, timeout=bounds.timeout(),
    ) as response:
        encodings = response.headers.get_list("Content-Encoding")
        if encodings and (len(encodings) != 1 or encodings[0].lower() != "identity"):
            fail()
        declared = None
        if "Content-Length" in response.headers:
            declared = _header(response, "Content-Length", MAX_TOTAL_BYTES)
            if declared > MAX_RESPONSE_BYTES or declared > MAX_TOTAL_BYTES - bounds.bytes:
                fail()
        chunks = []
        size = 0
        for chunk in response.iter_raw():
            bounds.check()
            size += len(chunk)
            bounds.bytes += len(chunk)
            if size > MAX_RESPONSE_BYTES or bounds.bytes > MAX_TOTAL_BYTES:
                fail()
            chunks.append(chunk)
        bounds.check()
        if declared is not None and declared != size:
            fail()
        raw = b"".join(chunks)
    # Close the bounded body before classification. Typed authorization failures
    # retain the existing CLI stage contract and keychain's 401-only restart.
    if response.status_code in (401, 403):
        raise httpx.HTTPStatusError(
            "Trakt source authorization failed", request=response.request, response=response,
        )
    if response.status_code != 200:
        fail()
    return response, raw


def _strict_history(headers, per_page, transport, bounds, before_request=None):
    try:
        if type(per_page) is not int or not 1 <= per_page <= MAX_ROWS:
            fail()
        rows = []
        seen = set()
        pinned = None
        page = 1
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            while True:
                response, raw = _read_page(client, headers, per_page, page, bounds, before_request)
                bounds.check()
                current = _header(response, "X-Pagination-Page", MAX_REQUESTS)
                limit = _header(response, "X-Pagination-Limit", MAX_ROWS)
                pages = _header(response, "X-Pagination-Page-Count", MAX_REQUESTS)
                total = _header(response, "X-Pagination-Item-Count", MAX_ROWS)
                if current != page or limit == 0:
                    fail()
                if total:
                    if pages != (total + limit - 1) // limit:
                        fail()
                elif page != 1 or pages not in (0, 1):
                    fail()
                found = (limit, pages, total)
                if pinned is None:
                    if pages > MAX_REQUESTS - bounds.requests + 1 or total > MAX_ROWS - bounds.rows:
                        fail()
                    pinned = found
                elif pinned != found:
                    fail()
                data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                  parse_constant=lambda _: fail())
                # Reject escaped unpaired surrogates even in intentionally
                # filtered fields, where they could make evidence ambiguous.
                json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
                bounds.check()
                expected = 0 if total == 0 else (limit if page < pages else total - limit * (pages - 1))
                if not isinstance(data, list) or len(data) != expected:
                    fail()
                for row in data:
                    bounds.check()
                    if bounds.rows >= MAX_ROWS:
                        fail()
                    bounds.rows += 1
                    if not isinstance(row, dict):
                        fail()
                    identity = row.get("id")
                    if type(identity) is not int or not 1 <= identity <= 2**64 - 1 or identity in seen:
                        fail()
                    seen.add(identity)
                    rows.append(row)
                if page >= pages:
                    bounds.check()
                    break
                page += 1
        bounds.check()
        return rows
    except httpx.HTTPStatusError:
        raise
    except Exception:
        fail()

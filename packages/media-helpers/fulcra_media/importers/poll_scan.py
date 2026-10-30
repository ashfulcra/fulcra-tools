"""Strict snapshot diagnostics for five standalone and Collect daemon poll routes.

Last.fm, Deezer, generic RSS, Letterboxd and Goodreads request strict scans
explicitly in their migrated callers. Importer non-strict defaults remain for
other consumers; daemon and standalone progress policies are separate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time
from urllib.parse import urlsplit, parse_qsl

import feedparser
import httpx

SOURCE_ERROR = "source scan failed"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_REQUESTS = 10000
MAX_ROWS = 1000000
SCAN_SECONDS = 300.0


def fail():
    raise RuntimeError(SOURCE_ERROR) from None


@dataclass
class ScanReport:
    started: bool = False
    finished: bool = False
    pages: int = 0
    rows_seen: int = 0
    intentional_exclusions: int = 0
    malformed_rows: int = 0
    enrichment_errors: int = 0
    configured_page_cap: bool = False
    configured_entry_cap: bool = False
    _phase: str = field(default="fresh", repr=False)
    _source: str | None = field(default=None, repr=False)
    _deadline: float = field(default=0, repr=False)
    _bytes: int = field(default=0, repr=False)
    _requests: int = field(default=0, repr=False)
    _rows: tuple = field(default=(), repr=False)
    _since: object = field(default=None, repr=False)

    def begin(self, source):
        if self._phase != "fresh" or self.started or self.finished:
            self.abort()
            fail()
        self.started = True
        self._source = source
        self._phase = "fetch"
        self._deadline = time.monotonic() + SCAN_SECONDS

    def check(self):
        if (
            self._phase not in ("fetch", "fetched", "normalize")
            or time.monotonic() >= self._deadline
        ):
            fail()

    def row(self):
        self.check()
        if self.rows_seen >= MAX_ROWS:
            fail()
        self.rows_seen += 1

    def fetched(self, rows):
        self.check()
        self._rows = tuple(rows)
        self._phase = "fetched"

    def normalize(self, source, rows):
        if self._source != source or self._phase != "fetched":
            fail()
        self.check()
        if len(rows) != len(self._rows) or any(a is not b for a, b in zip(rows, self._rows)):
            fail()
        self._phase = "normalize"

    def finish(self):
        self.check()
        if self._phase != "normalize":
            fail()
        self.finished = True
        self._phase = "finished"
        self._rows = ()

    def abort(self):
        self.finished = False
        self._phase = "failed"
        self._rows = ()


def require(report):
    if not isinstance(report, ScanReport):
        fail()
    return report


def decimal(value, *, signed=False, maximum=MAX_REQUESTS, length=10):
    if type(value) is int:
        result = value
    elif (
        isinstance(value, str)
        and len(value) <= length
        and re.fullmatch(r"-?[0-9]+" if signed else r"[0-9]+", value)
    ):
        result = int(value)
    else:
        fail()
    if (not signed and result < 0) or abs(result) > maximum:
        fail()
    return result


def timestamp(value):
    return decimal(value, signed=True, maximum=10**19, length=20)


def _settings(limit, max_pages, sleep):
    if type(limit) is not int or not 1 <= limit <= 200:
        fail()
    if max_pages is not None and (type(max_pages) is not int or not 1 <= max_pages <= MAX_REQUESTS):
        fail()
    import math

    if type(sleep) not in (int, float) or not math.isfinite(sleep) or sleep < 0:
        fail()


def pause(report, seconds):
    report.check()
    if seconds:
        time.sleep(min(seconds, max(0, report._deadline - time.monotonic())))
    report.check()


def body(client, url, report, *, params=None, timeout=30.0):
    report.check()
    import math

    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        fail()
    if report._requests >= MAX_REQUESTS:
        fail()
    report._requests += 1
    remaining = report._deadline - time.monotonic()
    with client.stream(
        "GET",
        url,
        params=params,
        headers={"Accept-Encoding": "identity"},
        timeout=min(30.0, timeout, remaining),
    ) as response:
        encoding = response.headers.get("Content-Encoding", "identity").lower()
        if encoding != "identity":
            fail()
        declared = response.headers.get("Content-Length")
        if declared is not None:
            size = decimal(declared, maximum=MAX_TOTAL_BYTES, length=10)
            if size > MAX_RESPONSE_BYTES or size > MAX_TOTAL_BYTES - report._bytes:
                fail()
        chunks = []
        size = 0
        for chunk in response.iter_raw():
            report.check()
            size += len(chunk)
            report._bytes += len(chunk)
            if size > MAX_RESPONSE_BYTES or report._bytes > MAX_TOTAL_BYTES:
                fail()
            chunks.append(chunk)
        report.check()
        # Count HTTP-error bodies too, and never follow a redirect.
        if not 200 <= response.status_code < 300:
            fail()
        return b"".join(chunks)


def document(client, url, report, *, params):
    raw = body(client, url, report, params=params)
    report.check()
    data = json.loads(raw)
    report.check()
    if not isinstance(data, dict) or "error" in data:
        fail()
    return data


def lastfm_rows(creds, *, report, since, until, limit, max_pages, sleep, transport):
    from .lastfm import LASTFM_BASE

    report = require(report)
    report.begin("lastfm")
    try:
        _settings(limit, max_pages, sleep)
        rows = []
        total = None
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            page = 1
            while True:
                params = dict(
                    method="user.getRecentTracks",
                    user=creds["username"],
                    api_key=creds["api_key"],
                    format="json",
                    limit=limit,
                    page=page,
                )
                if since is not None:
                    params["from"] = int(since.timestamp())
                if until is not None:
                    params["to"] = int(until.timestamp())
                data = document(client, LASTFM_BASE, report, params=params)
                recent = data.get("recenttracks")
                if (
                    not isinstance(recent, dict)
                    or "track" not in recent
                    or not isinstance(recent.get("@attr"), dict)
                ):
                    fail()
                tracks = recent["track"]
                if isinstance(tracks, dict):
                    tracks = [tracks]
                if not isinstance(tracks, list):
                    fail()
                attrs = recent["@attr"]
                found = decimal(attrs.get("totalPages"))
                if total is not None and total != found:
                    fail()
                total = found
                if "page" in attrs and decimal(attrs["page"]) != page:
                    fail()
                if total == 0 and (page != 1 or tracks):
                    fail()
                if total and page > total:
                    fail()
                report.pages += 1
                for row in tracks:
                    report.row()
                    rows.append(row)
                if max_pages is not None and page >= max_pages:
                    report.configured_page_cap = True
                    break
                if page >= total:
                    break
                page += 1
                pause(report, sleep)
        report.fetched(rows)
        return rows
    except BaseException:
        report.abort()
        fail()


def deezer_next(value, token, position, limit):
    if not isinstance(value, str):
        fail()
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or parts.hostname != "api.deezer.com"
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or parts.path != "/user/me/history"
    ):
        fail()
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    query = dict(pairs)
    if len(query) != len(pairs) or set(query) - {"index", "limit", "access_token"}:
        fail()
    index = decimal(query.get("index"), maximum=10**10 - 1)
    if index <= position:
        fail()
    page_limit = decimal(query["limit"], maximum=200) if "limit" in query else limit
    if page_limit < 1 or ("access_token" in query and query["access_token"] != token):
        fail()
    return index, page_limit


def deezer_rows(creds, *, report, since, limit, max_pages, sleep, transport):
    from .deezer import DEEZER_HISTORY_URL

    report = require(report)
    report.begin("deezer")
    try:
        _settings(limit, max_pages, sleep)
        token = creds["access_token"]
        rows = []
        position = 0
        report._since = since
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            while True:
                data = document(
                    client,
                    DEEZER_HISTORY_URL,
                    report,
                    params={"access_token": token, "limit": limit, "index": position},
                )
                items = data.get("data")
                if not isinstance(items, list):
                    fail()
                report.pages += 1
                for row in items:
                    report.row()
                    rows.append(row)
                next_page = data.get("next")
                if next_page not in (None, ""):
                    position, limit = deezer_next(next_page, token, position, limit)
                if max_pages is not None and report.pages >= max_pages:
                    report.configured_page_cap = True
                    break
                if next_page in (None, ""):
                    break
                pause(report, sleep)
        report.fetched(rows)
        return rows
    except BaseException:
        report.abort()
        fail()


def normalize_api(tracks, *, report, source, normalizer):
    from .lastfm import MIN_VALID_UTS

    report = require(report)
    try:
        rows = list(tracks)
        report.normalize(source, rows)
        events = []
        for row in rows:
            report.check()
            if (
                source == "lastfm"
                and isinstance(row, dict)
                and isinstance(row.get("@attr"), dict)
                and row["@attr"].get("nowplaying") == "true"
            ):
                report.intentional_exclusions += 1
                continue
            try:
                if not isinstance(row, dict):
                    raise ValueError
                raw = row.get("date", {}).get("uts") if source == "lastfm" else row.get("timestamp")
                ts = timestamp(raw)
                if source == "lastfm" and ts < MIN_VALID_UTS:
                    raise ValueError
                event = normalizer(row)
            except (ValueError, TypeError, AttributeError, OverflowError, OSError, RuntimeError):
                event = None
            report.check()
            if event is None:
                report.malformed_rows += 1
            elif report._since is not None and event.start_time < report._since:
                report.intentional_exclusions += 1
            else:
                events.append(event)
        report.finish()
        return events
    except BaseException:
        report.abort()
        fail()


def feed_document(url, *, report, transport, timeout=30):
    report = require(report)
    report.begin("feed")
    try:
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            raw = body(client, url, report, timeout=timeout)
        report.check()
        parsed = feedparser.parse(raw)
        report.check()
        if (
            parsed.get("bozo")
            or parsed.get("version")
            not in (
                "rss",
                "rss10",
                "rss20",
                "rss091n",
                "rss091u",
                "rss092",
                "rss093",
                "rss094",
                "atom10",
                "atom03",
            )
            or not isinstance(parsed.get("entries"), list)
        ):
            fail()
        report.pages = 1
        for row in parsed.entries:
            report.row()
        report.fetched(parsed.entries)
        return parsed
    except BaseException:
        report.abort()
        fail()


def normalize_feed_rows(parsed, *, report, normalizer, malformed=None):
    report = require(report)
    try:
        rows = list(parsed.entries)
        report.normalize("feed", rows)
        events = []
        for entry in rows:
            report.check()
            if malformed is not None and malformed(entry):
                report.malformed_rows += 1
            try:
                event = normalizer(entry)
            except Exception:
                report.enrichment_errors += 1
                raise
            report.check()
            if event is None:
                report.malformed_rows += 1
            else:
                events.append(event)
        report.finish()
        return events
    except BaseException:
        report.abort()
        fail()

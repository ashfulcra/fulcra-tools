"""Hermetic strict source snapshots; no provider accounts or credentials."""

import json
from datetime import datetime, timezone
import httpx
import pytest
from fulcra_media.importers import lastfm, deezer, generic_rss


class Bytes(httpx.SyncByteStream):
    def __init__(self, body):
        self.body = body

    def __iter__(self):
        yield self.body


def response(body, **kwargs):
    return httpx.Response(200, stream=Bytes(json.dumps(body).encode()), **kwargs)


def track(ts="1700000000"):
    return {"name": "Song", "artist": {"#text": "Artist"}, "date": {"uts": ts}}


def test_lastfm_affirmative_terminal_normalization_and_parity():
    from fulcra_media.importers.poll_scan import ScanReport

    report = ScanReport()
    transport = httpx.MockTransport(
        lambda r: response(
            {"recenttracks": {"track": [track()], "@attr": {"totalPages": "1", "page": "1"}}}
        )
    )
    raw = lastfm.fetch_recent_tracks(
        {"username": "synthetic", "api_key": "secret"},
        transport=transport,
        strict_scan=True,
        scan_report=report,
    )
    events = list(lastfm.normalize_history(raw, strict_scan=True, scan_report=report))
    assert events == list(lastfm.normalize_history([track()]))
    assert report.finished and report.rows_seen == 1 and report.pages == 1
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        list(lastfm.normalize_history([], strict_scan=True, scan_report=report))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"recenttracks": {}},
        {"recenttracks": {"track": [], "@attr": {"totalPages": True}}},
        {"recenttracks": {"track": [], "@attr": {"totalPages": "+1"}}},
        {"recenttracks": {"track": [], "@attr": {"totalPages": "١"}}},
        {"error": 1, "message": "secret"},
    ],
)
def test_invalid_lastfm_never_finishes(payload):
    from fulcra_media.importers.poll_scan import ScanReport

    report = ScanReport()
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        list(
            lastfm.fetch_recent_tracks(
                {"username": "synthetic", "api_key": "secret"},
                transport=httpx.MockTransport(lambda r: response(payload)),
                strict_scan=True,
                scan_report=report,
            )
        )
    assert not report.finished


def test_missing_date_is_malformed_nowplaying_intentional():
    from fulcra_media.importers.poll_scan import ScanReport

    report = ScanReport()
    rows = [track(), {"name": "bad"}, {"@attr": {"nowplaying": "true"}}]
    transport = httpx.MockTransport(
        lambda r: response({"recenttracks": {"track": rows, "@attr": {"totalPages": "1"}}})
    )
    events = list(
        lastfm.normalize_history(
            lastfm.fetch_recent_tracks(
                {"username": "synthetic", "api_key": "secret"},
                transport=transport,
                strict_scan=True,
                scan_report=report,
            ),
            strict_scan=True,
            scan_report=report,
        )
    )
    assert len(events) == 1
    assert report.finished and report.malformed_rows == 1 and report.intentional_exclusions == 1


def test_deezer_exhausts_later_page_even_if_old_row():
    from fulcra_media.importers.poll_scan import ScanReport

    calls = []

    def handler(request):
        calls.append(request)
        row = {"id": 1, "title": "Song", "artist": {"name": "Artist"}, "timestamp": "1700000000"}
        if len(calls) == 1:
            return response(
                {
                    "data": [{**row, "timestamp": "1"}, row],
                    "next": "https://api.deezer.com/user/me/history?index=2&limit=50",
                }
            )
        return response({"data": [{**row, "id": 2}], "next": None})

    report = ScanReport()
    events = list(
        deezer.normalize_history(
            deezer.fetch_history(
                {"access_token": "secret"},
                since=datetime(2020, 1, 1, tzinfo=timezone.utc),
                transport=httpx.MockTransport(handler),
                sleep_between_pages=0,
                strict_scan=True,
                scan_report=report,
            ),
            strict_scan=True,
            scan_report=report,
        )
    )
    assert len(events) == 2 and len(calls) == 2 and report.finished and report.rows_seen == 3


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.invalid/user/me/history?index=1",
        "https://api.deezer.com/user/me/history?index=0",
        "https://api.deezer.com/user/me/history?index=1&access_token=other",
        "https://api.deezer.com/user/me/history?index=1&index=2",
        "https://api.deezer.com/user/me/history?index=1&unknown=2",
        "https://api.deezer.com:444/user/me/history?index=1",
    ],
)
def test_deezer_rejects_untrusted_next_before_contact(url):
    from fulcra_media.importers.poll_scan import ScanReport

    calls = []

    def handler(request):
        calls.append(request)
        return response({"data": [], "next": url})

    report = ScanReport()
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        list(
            deezer.fetch_history(
                {"access_token": "secret"},
                transport=httpx.MockTransport(handler),
                strict_scan=True,
                scan_report=report,
            )
        )
    assert len(calls) == 1 and not report.finished


@pytest.mark.parametrize(
    "body,valid",
    [
        (b"<html><body>secret</body></html>", False),
        (b'<rss version="2.0"><channel><title>empty</title></channel></rss>', True),
        (b'<rss version="2.0"><channel>', False),
    ],
)
def test_feed_shape(body, valid):
    from fulcra_media.importers.poll_scan import ScanReport

    report = ScanReport()
    transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=Bytes(body)))
    if valid:
        assert (
            list(
                generic_rss.normalize_feed(
                    "https://synthetic.invalid/feed",
                    service="rss",
                    category="read",
                    transport=transport,
                    strict_scan=True,
                    scan_report=report,
                )
            )
            == []
        )
        assert report.finished
    else:
        with pytest.raises(RuntimeError, match="^source scan failed$"):
            list(
                generic_rss.normalize_feed(
                    "https://synthetic.invalid/feed",
                    service="rss",
                    category="read",
                    transport=transport,
                    strict_scan=True,
                    scan_report=report,
                )
            )
        assert not report.finished


@pytest.mark.parametrize("source", ["lastfm", "deezer"])
@pytest.mark.parametrize("kind", ["json", "redirect", "encoding", "page-two"])
def test_failure_is_static_and_no_partial_snapshot(source, kind):
    from fulcra_media.importers.poll_scan import ScanReport

    report = ScanReport()
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["accept-encoding"] == "identity"
        if kind == "redirect":
            return httpx.Response(
                302,
                headers={"location": "https://attacker.invalid/secret"},
                stream=Bytes(b"private"),
            )
        if kind == "encoding":
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=Bytes(b"private")
            )
        if kind == "json" or len(calls) == 2:
            return httpx.Response(200, stream=Bytes(b"{private invalid json"))
        if source == "lastfm":
            return response({"recenttracks": {"track": [track()], "@attr": {"totalPages": "2"}}})
        return response(
            {
                "data": [
                    {
                        "id": 1,
                        "title": "Song",
                        "artist": {"name": "Artist"},
                        "timestamp": "1700000000",
                    }
                ],
                "next": "https://api.deezer.com/user/me/history?index=1",
            }
        )

    transport = httpx.MockTransport(handler)
    if source == "lastfm":
        raw = lastfm.fetch_recent_tracks(
            {"username": "synthetic", "api_key": "secret"},
            transport=transport,
            strict_scan=True,
            scan_report=report,
            sleep_between_pages=0,
        )
    else:
        raw = deezer.fetch_history(
            {"access_token": "secret"},
            transport=transport,
            strict_scan=True,
            scan_report=report,
            sleep_between_pages=0,
        )
    normalizer = lastfm.normalize_history if source == "lastfm" else deezer.normalize_history
    delivered = []
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        delivered.extend(normalizer(raw, strict_scan=True, scan_report=report))
    assert not delivered and not report.finished
    assert len(calls) == (2 if kind == "page-two" else 1)
    assert all(request.url.host != "attacker.invalid" for request in calls)


@pytest.mark.parametrize(
    "limit_name", ["MAX_RESPONSE_BYTES", "MAX_TOTAL_BYTES", "MAX_REQUESTS", "MAX_ROWS"]
)
def test_inclusive_hard_bounds_and_one_over(monkeypatch, limit_name):
    from fulcra_media.importers import poll_scan

    payload = {"recenttracks": {"track": [track()], "@attr": {"totalPages": "1"}}}
    size = len(json.dumps(payload).encode())
    boundary = size if "BYTES" in limit_name else 1
    monkeypatch.setattr(poll_scan, limit_name, boundary)

    def execute():
        report = poll_scan.ScanReport()
        raw = lastfm.fetch_recent_tracks(
            {"username": "synthetic", "api_key": "secret"},
            transport=httpx.MockTransport(lambda r: response(payload)),
            strict_scan=True,
            scan_report=report,
        )
        return list(lastfm.normalize_history(raw, strict_scan=True, scan_report=report)), report

    events, report = execute()
    assert len(events) == 1 and report.finished
    monkeypatch.setattr(poll_scan, limit_name, boundary - 1)
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        execute()


def test_cooperative_deadline_refuses_after_parser(monkeypatch):
    from fulcra_media.importers import poll_scan

    clock = [0.0]
    monkeypatch.setattr(poll_scan.time, "monotonic", lambda: clock[0])
    original = poll_scan.json.loads

    def slow(raw):
        result = original(raw)
        clock[0] = 301.0
        return result

    monkeypatch.setattr(poll_scan.json, "loads", slow)
    report = poll_scan.ScanReport()
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        list(
            lastfm.fetch_recent_tracks(
                {"username": "synthetic", "api_key": "secret"},
                transport=httpx.MockTransport(
                    lambda r: response(
                        {"recenttracks": {"track": [], "@attr": {"totalPages": "0"}}}
                    )
                ),
                strict_scan=True,
                scan_report=report,
            )
        )
    assert not report.finished


@pytest.mark.parametrize("source", ["letterboxd", "goodreads", "generic"])
def test_feed_default_strict_identity_and_enrichment_parity(source):
    from pathlib import Path
    from fulcra_media.importers import letterboxd, goodreads
    from fulcra_media.importers.poll_scan import ScanReport

    filename = {
        "letterboxd": "letterboxd_sample.xml",
        "goodreads": "goodreads_sample.xml",
        "generic": "generic_atom_sample.xml",
    }[source]
    raw = (Path(__file__).parent / "fixtures" / filename).read_bytes()
    transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=Bytes(raw)))

    def load(**kw):
        if source == "letterboxd":
            return list(letterboxd.fetch_diary("synthetic", transport=transport, **kw))
        if source == "goodreads":
            return list(goodreads.fetch_diary("12345", transport=transport, **kw))
        return list(
            generic_rss.normalize_feed(
                "https://synthetic.invalid/feed?secret=private",
                service="rss",
                category="read",
                transport=transport,
                **kw,
            )
        )

    default = load()
    report = ScanReport()
    strict = load(strict_scan=True, scan_report=report)
    assert strict == default and strict and report.finished


@pytest.mark.parametrize(
    "callback", ["extract_fingerprint", "extra_external_ids", "extract_extra_source_ids"]
)
def test_callback_exception_is_fatal_before_any_events(callback):
    from pathlib import Path
    from fulcra_media.importers.poll_scan import ScanReport

    raw = (Path(__file__).parent / "fixtures" / "generic_atom_sample.xml").read_bytes()
    report = ScanReport()

    def failed(*args):
        raise ValueError("https://private.invalid/?token=secret")

    result = []
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        result.extend(
            generic_rss.normalize_feed(
                "https://synthetic.invalid/feed",
                service="rss",
                category="read",
                transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Bytes(raw))),
                strict_scan=True,
                scan_report=report,
                **{callback: failed},
            )
        )
    assert not result and not report.finished and report.enrichment_errors == 1


def test_lastfm_empty_positive_total_still_fetches_and_total_change_refused():
    from fulcra_media.importers.poll_scan import ScanReport

    calls = []

    def handler(request):
        calls.append(request)
        return response(
            {
                "recenttracks": {
                    "track": [],
                    "@attr": {"totalPages": "2" if len(calls) == 1 else "3"},
                }
            }
        )

    report = ScanReport()
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        list(
            lastfm.fetch_recent_tracks(
                {"username": "synthetic", "api_key": "secret"},
                transport=httpx.MockTransport(handler),
                strict_scan=True,
                scan_report=report,
                sleep_between_pages=0,
            )
        )
    assert len(calls) == 2 and not report.finished


def test_exact_frozen_byte_request_row_limits(monkeypatch):
    from fulcra_media.importers import poll_scan

    assert (
        poll_scan.MAX_RESPONSE_BYTES,
        poll_scan.MAX_TOTAL_BYTES,
        poll_scan.MAX_REQUESTS,
        poll_scan.MAX_ROWS,
        poll_scan.SCAN_SECONDS,
    ) == (8388608, 67108864, 10000, 1000000, 300.0)
    report = poll_scan.ScanReport()
    report.begin("test")
    raw = b"x" * 8388608
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Bytes(raw)))
    ) as client:
        for _ in range(8):
            assert len(poll_scan.body(client, "https://synthetic.invalid", report)) == 8388608
        assert report._bytes == 67108864
        with pytest.raises(RuntimeError, match="^source scan failed$"):
            poll_scan.body(client, "https://synthetic.invalid", report)
    calls = []
    report = poll_scan.ScanReport()
    report.begin("test")
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (calls.append(r), httpx.Response(200, stream=Bytes(b"")))[1]
        )
    ) as client:
        for _ in range(10000):
            poll_scan.body(client, "https://synthetic.invalid", report)
        with pytest.raises(RuntimeError, match="^source scan failed$"):
            poll_scan.body(client, "https://synthetic.invalid", report)
    assert len(calls) == 10000
    report = poll_scan.ScanReport()
    report.begin("test")
    for _ in range(1000000):
        report.row()
    assert report.rows_seen == 1000000
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        report.row()
    assert report.rows_seen == 1000000


@pytest.mark.parametrize("metadata", [0, "0"])
def test_zero_pages_explicit_empty_is_valid(metadata):
    from fulcra_media.importers.poll_scan import ScanReport

    report = ScanReport()
    raw = lastfm.fetch_recent_tracks(
        {"username": "synthetic", "api_key": "secret"},
        transport=httpx.MockTransport(
            lambda r: response({"recenttracks": {"track": [], "@attr": {"totalPages": metadata}}})
        ),
        strict_scan=True,
        scan_report=report,
    )
    assert list(lastfm.normalize_history(raw, strict_scan=True, scan_report=report)) == []
    assert report.finished and report.pages == 1


def test_abandoned_fetch_or_exception_report_cannot_start_again():
    from fulcra_media.importers.poll_scan import ScanReport

    transport = httpx.MockTransport(
        lambda r: response(
            {"recenttracks": {"track": [track(), track()], "@attr": {"totalPages": 1}}}
        )
    )
    report = ScanReport()
    raw = lastfm.fetch_recent_tracks(
        {"username": "synthetic", "api_key": "secret"},
        transport=transport,
        strict_scan=True,
        scan_report=report,
    )
    next(raw)
    raw.close()
    assert not report.finished
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        list(
            lastfm.fetch_recent_tracks(
                {"username": "synthetic", "api_key": "secret"},
                transport=transport,
                strict_scan=True,
                scan_report=report,
            )
        )


def test_goodreads_bad_nonempty_read_at_is_diagnostic_but_empty_fallback_supported():
    from fulcra_media.importers import goodreads
    from fulcra_media.importers.poll_scan import ScanReport

    raw = b'<rss version="2.0"><channel><title>Synthetic</title><item><title>Book</title><guid>one</guid><pubDate>Tue, 14 Nov 2023 00:00:00 GMT</pubDate><user_read_at>not a date</user_read_at></item><item><title>Other</title><guid>two</guid><pubDate>Tue, 14 Nov 2023 00:00:00 GMT</pubDate><user_read_at></user_read_at></item></channel></rss>'
    report = ScanReport()
    events = list(
        goodreads.fetch_diary(
            "12345",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Bytes(raw))),
            strict_scan=True,
            scan_report=report,
        )
    )
    assert len(events) == 2 and report.finished and report.malformed_rows == 1


@pytest.mark.parametrize(
    "value", [True, 1.0, " 1700000000", "+1700000000", "1e9", "١٧٠٠٠٠٠٠٠٠", "9" * 21]
)
def test_strict_timestamp_grammar_malformed_without_coercion(value):
    from fulcra_media.importers.poll_scan import ScanReport

    report = ScanReport()
    raw = lastfm.fetch_recent_tracks(
        {"username": "synthetic", "api_key": "secret"},
        transport=httpx.MockTransport(
            lambda r: response(
                {"recenttracks": {"track": [track(value)], "@attr": {"totalPages": 1}}}
            )
        ),
        strict_scan=True,
        scan_report=report,
    )
    assert list(lastfm.normalize_history(raw, strict_scan=True, scan_report=report)) == []
    assert report.finished and report.malformed_rows == 1


def test_failed_reuse_invalidates_terminal_report():
    from fulcra_media.importers.poll_scan import ScanReport

    report = ScanReport()

    def fetch():
        return lastfm.fetch_recent_tracks(
            {"username": "synthetic", "api_key": "secret"},
            transport=httpx.MockTransport(
                lambda r: response({"recenttracks": {"track": [], "@attr": {"totalPages": 0}}})
            ),
            strict_scan=True,
            scan_report=report,
        )

    assert list(lastfm.normalize_history(fetch(), strict_scan=True, scan_report=report)) == []
    assert report.finished
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        list(fetch())
    assert not report.finished


def test_deezer_clean_snapshot_matches_default_identity_and_enrichment():
    from fulcra_media.importers.poll_scan import ScanReport

    row = {
        "id": 7,
        "title": "Song",
        "artist": {"name": "Artist"},
        "album": {"title": "Album"},
        "timestamp": "1700000000",
    }
    transport = httpx.MockTransport(lambda r: response({"data": [row]}))
    default = list(
        deezer.normalize_history(
            deezer.fetch_history({"access_token": "synthetic"}, transport=transport)
        )
    )
    report = ScanReport()
    strict = list(
        deezer.normalize_history(
            deezer.fetch_history(
                {"access_token": "synthetic"},
                transport=transport,
                strict_scan=True,
                scan_report=report,
            ),
            strict_scan=True,
            scan_report=report,
        )
    )
    assert strict == default and strict and report.finished


def test_strict_feed_honors_shorter_existing_timeout():
    from fulcra_media.importers.poll_scan import ScanReport

    timeouts = []

    def handle(request):
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, stream=Bytes(b'<rss version="2.0"><channel/></rss>'))

    generic_rss.fetch_feed(
        "https://synthetic.invalid/feed",
        transport=httpx.MockTransport(handle),
        timeout=1.0,
        strict_scan=True,
        scan_report=ScanReport(),
    )
    assert all(value <= 1.0 for value in timeouts[0].values())

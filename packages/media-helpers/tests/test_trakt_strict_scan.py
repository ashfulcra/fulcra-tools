"""Hermetic full-history completeness and resource boundaries for Trakt."""

from types import SimpleNamespace

import httpx
import pytest

from fulcra_media.importers import trakt


def metadata(page=1, limit=2, total=3, pages=2):
    return {
        "X-Pagination-Page": str(page),
        "X-Pagination-Limit": str(limit),
        "X-Pagination-Item-Count": str(total),
        "X-Pagination-Page-Count": str(pages),
    }


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    # MockTransport does not isolate auth subprocesses or the old file path.
    monkeypatch.setattr(trakt, "load_creds", lambda: pytest.fail("real legacy credentials"))
    monkeypatch.setattr(trakt, "save_creds", lambda _: pytest.fail("real legacy persistence"))
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: pytest.fail("unfenced auth POST"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *_: pytest.fail("real HTTP"))


def install_source(monkeypatch, handler):
    actual = httpx.Client
    clients = []

    def streamed(request):
        response = handler(request)
        if response.is_stream_consumed:
            response = httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=httpx.ByteStream(response.content),
            )
        return response

    def client(**kw):
        kw["transport"] = httpx.MockTransport(streamed)
        instance = actual(**kw)
        clients.append(instance)
        return instance

    monkeypatch.setattr(
        trakt, "httpx", SimpleNamespace(Client=client, HTTPStatusError=httpx.HTTPStatusError)
    )
    return clients


@pytest.mark.parametrize("mode", ["missing-headers", "premature-empty"])
def test_incomplete_actual_fetch_never_yields_prefix(monkeypatch, mode):
    requests = []

    def source(request):
        requests.append(request)
        page = int(request.url.params["page"])
        if mode == "missing-headers":
            return httpx.Response(200, json=[{"id": 1}])
        return httpx.Response(
            200, headers=metadata(page=page), json=[{"id": 1}, {"id": 2}] if page == 1 else []
        )

    clients = install_source(monkeypatch, source)
    scan = trakt.fetch_history_with_headers({"Authorization": "Bearer synthetic"}, strict_scan=True)
    with pytest.raises(RuntimeError, match="source scan failed"):
        next(scan)
    assert all(client.is_closed for client in clients)
    assert [r.url.params["page"] for r in requests] == (
        ["1"] if mode == "missing-headers" else ["1", "2"]
    )


def fetch(monkeypatch, handler, **kw):
    clients = install_source(monkeypatch, handler)
    try:
        return list(
            trakt.fetch_history_with_headers(
                {"Authorization": "Bearer synthetic"}, strict_scan=True, **kw
            )
        )
    finally:
        assert clients and all(c.is_closed for c in clients)


def test_effective_clamp_three_pages_and_exact_remainder(monkeypatch):
    requests = []

    def source(request):
        requests.append(request)
        page = int(request.url.params["page"])
        assert request.url.host == "api.trakt.tv" and request.url.path == "/sync/history"
        assert request.url.params["limit"] == "1000"
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["Authorization"] == "Bearer synthetic"
        assert request.extensions["timeout"]["read"] <= 30
        ids = {1: [8, 2], 2: [19, 7], 3: [2**64 - 1]}[page]
        return httpx.Response(
            200, headers=metadata(page, total=5, pages=3), json=[{"id": i} for i in ids]
        )

    assert [r["id"] for r in fetch(monkeypatch, source)] == [8, 2, 19, 7, 2**64 - 1]
    assert [r.url.params["page"] for r in requests] == ["1", "2", "3"]


@pytest.mark.parametrize("pages", [0, 1])
def test_explicit_empty_and_leading_zero_headers(monkeypatch, pages):
    headers = {k.lower(): "000" + v for k, v in metadata(total=0, pages=pages).items()}
    assert fetch(monkeypatch, lambda _: httpx.Response(200, headers=headers, json=[])) == []


@pytest.mark.parametrize("header", list(metadata()))
@pytest.mark.parametrize(
    "value", [None, "", "-1", "+1", " 1", "1 ", "1.0", "1,1", "١", "00000000001"]
)
def test_missing_or_ambiguous_decimal_metadata(monkeypatch, header, value):
    headers = metadata(total=1, pages=1)
    if value is None:
        headers.pop(header)
    else:
        headers[header] = value
    # HTTPX rejects non-ASCII str headers itself; raw bytes preserve the test.
    raw_headers = [(k.encode(), v.encode()) for k, v in headers.items()]
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        fetch(monkeypatch, lambda _: httpx.Response(200, headers=raw_headers, json=[{"id": 1}]))


@pytest.mark.parametrize("header", list(metadata()))
def test_duplicate_raw_headers_even_equal_are_not_evidence(monkeypatch, header):
    headers = list(metadata(total=1, pages=1).items())
    headers.append((header.lower(), dict(headers)[header]))
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        fetch(monkeypatch, lambda _: httpx.Response(200, headers=headers, json=[{"id": 1}]))


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        4,
        [None],
        [[]],
        [True],
        [{"id": True}],
        [{"id": 0}],
        [{"id": -1}],
        [{"id": 2**64}],
        [{"id": "1"}],
        [{"id": 1.0}],
        [{}],
    ],
)
def test_raw_shape_and_identity_fail_before_type_filtering(monkeypatch, data):
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        fetch(
            monkeypatch,
            lambda _: httpx.Response(200, headers=metadata(total=1, pages=1), json=data),
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'[{"id":1,"id":2}]',
        b'[{"id":1,"nested":{"a":1,"a":2}}]',
        b'[{"id":1,"x":1e999}]',
        b'[{"id":1,"x":NaN}]',
        b'[{"id":1,"x":Infinity}]',
        b'[{"id":1,"x":-Infinity}]',
        b'[{"id":1,"x":"\\ud800"}]',
        b'[{"id":1,"x":"\xff"}]',
        b"[",
    ],
)
def test_ambiguous_json_unicode_and_nonfinite_refused(monkeypatch, raw):
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        fetch(
            monkeypatch,
            lambda _: httpx.Response(200, headers=metadata(total=1, pages=1), content=raw),
        )


@pytest.mark.parametrize(
    "mode",
    [
        "wrong-page",
        "limit-change",
        "total-change",
        "pages-change",
        "repeated-page",
        "duplicate-within",
        "short-first",
        "extra-last",
        "empty-last",
        "late-status",
    ],
)
def test_contradiction_on_any_page_yields_nothing(monkeypatch, mode):
    def source(request):
        page = int(request.url.params["page"])
        headers = metadata(page)
        data = [{"id": 1}, {"id": 2}] if page == 1 else [{"id": 3}]
        if mode == "wrong-page":
            headers["X-Pagination-Page"] = "2"
        if page == 2:
            if mode == "limit-change":
                headers["X-Pagination-Limit"] = "3"
            if mode == "total-change":
                headers["X-Pagination-Item-Count"] = "4"
            if mode == "pages-change":
                headers["X-Pagination-Page-Count"] = "3"
            if mode == "repeated-page":
                data = [{"id": 1}]
            if mode == "extra-last":
                data.append({"id": 4})
            if mode == "empty-last":
                data = []
            if mode == "late-status":
                return httpx.Response(503, text="SECRET")
        if page == 1:
            if mode == "duplicate-within":
                data = [{"id": 1}, {"id": 1}]
            if mode == "short-first":
                data.pop()
        return httpx.Response(200, headers=headers, json=data)

    clients = install_source(monkeypatch, source)
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        next(trakt.fetch_history_with_headers({}, strict_scan=True))
    assert all(c.is_closed for c in clients)


@pytest.mark.parametrize(
    "page,limit,total,pages,data",
    [
        (1, 0, 0, 0, []),
        (1, 2, 0, 2, []),
        (2, 2, 0, 1, []),
        (1, 2, 0, 1, [{"id": 1}]),
        (1, 2, 1, 0, []),
        (1, 2, 3, 1, [{"id": 1}]),
    ],
)
def test_inconsistent_empty_or_page_arithmetic(monkeypatch, page, limit, total, pages, data):
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        fetch(
            monkeypatch,
            lambda _: httpx.Response(200, headers=metadata(page, limit, total, pages), json=data),
        )


@pytest.mark.parametrize("status", [201, 204, 301, 302, 307, 401, 403, 429, 500])
def test_exact_status_and_static_closed_error(monkeypatch, status):
    responses = []
    requests = []

    def source(request):
        requests.append(request)
        response = httpx.Response(
            status,
            headers={"Location": "https://hostile.invalid/SECRET"},
            stream=httpx.ByteStream(b"SECRET"),
        )
        responses.append(response)
        return response

    error = httpx.HTTPStatusError if status in (401, 403) else RuntimeError
    with pytest.raises(error) as caught:
        fetch(monkeypatch, source)
    assert "SECRET" not in str(caught.value) and "https://" not in str(caught.value)
    assert len(requests) == 1 and all(r.is_closed for r in responses)


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, after=lambda: None, error=False):
        self.chunks, self.after, self.error, self.closed = chunks, after, error, False

    def __iter__(self):
        for chunk in self.chunks:
            self.after()
            yield chunk
        if self.error:
            raise httpx.ReadError("SECRET")

    def close(self):
        self.closed = True


@pytest.mark.parametrize("status", [200, 401, 403, 429, 500])
@pytest.mark.parametrize(
    "mode", ["encoding", "duplicate-encoding", "oversize", "declared", "stream-error"]
)
def test_body_failure_is_not_refreshable_and_closes(monkeypatch, status, mode):
    monkeypatch.setattr(trakt, "MAX_RESPONSE_BYTES", 32)
    headers = metadata(total=1, pages=1)
    headers = list(headers.items())
    if mode == "encoding":
        headers.append(("Content-Encoding", "gzip"))
    if mode == "duplicate-encoding":
        headers += [("Content-Encoding", "identity")] * 2
    if mode == "declared":
        headers.append(("Content-Length", "33"))
    stream = Chunks([b"x" * (33 if mode == "oversize" else 5)], error=mode == "stream-error")
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        fetch(monkeypatch, lambda _: httpx.Response(status, headers=headers, stream=stream))
    assert stream.closed


@pytest.mark.parametrize("cap", ["MAX_RESPONSE_BYTES", "MAX_TOTAL_BYTES"])
@pytest.mark.parametrize("over", [False, True])
def test_raw_bytes_exact_cap_requires_terminal_evidence(monkeypatch, cap, over):
    raw = b'[{"id":1}]'
    monkeypatch.setattr(trakt, cap, len(raw) - int(over))

    def source(_):
        return httpx.Response(
            200, headers=metadata(total=1, pages=1), stream=Chunks([raw[:4], raw[4:]])
        )

    if over:
        with pytest.raises(RuntimeError, match="^source scan failed$"):
            fetch(monkeypatch, source)
    else:
        assert fetch(monkeypatch, source) == [{"id": 1}]


@pytest.mark.parametrize("cap,value", [("MAX_REQUESTS", 2), ("MAX_ROWS", 3)])
@pytest.mark.parametrize("over", [False, True])
def test_requests_and_rows_inclusive_scope(monkeypatch, cap, value, over):
    monkeypatch.setattr(trakt, cap, value - int(over))
    requests = []

    def source(request):
        requests.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(
            200, headers=metadata(page), json=[{"id": 1}, {"id": 2}] if page == 1 else [{"id": 3}]
        )

    if over:
        with pytest.raises(RuntimeError, match="^source scan failed$"):
            fetch(monkeypatch, source, per_page=2)
        assert len(requests) == 1  # metadata scope refuses before looping
    else:
        assert len(fetch(monkeypatch, source, per_page=2)) == 3 and len(requests) == 2


@pytest.mark.parametrize("where", ["before-request", "stream", "decode", "row", "return"])
def test_cooperative_deadline_around_slow_operations(monkeypatch, where):
    clock = [0.0]
    monkeypatch.setattr(trakt.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(trakt, "SCAN_SECONDS", 5.0)
    bounds = trakt._ScanBounds()
    if where == "before-request":
        clock[0] = 5
    if where == "decode":
        original = trakt.json.loads

        def decode(*a, **kw):
            result = original(*a, **kw)
            clock[0] = 5
            return result

        monkeypatch.setattr(trakt.json, "loads", decode)
    if where == "row":
        original = trakt._unique_object

        class SlowRow(dict):
            def get(self, key, *a):
                clock[0] = 5
                return super().get(key, *a)

        monkeypatch.setattr(trakt, "_unique_object", lambda pairs: SlowRow(original(pairs)))
    if where == "return":
        original_close = httpx.MockTransport.close

        def close(transport):
            original_close(transport)
            clock[0] = 5

        monkeypatch.setattr(httpx.MockTransport, "close", close)
    stream = Chunks(
        [b'[{"id":1}]'], after=lambda: clock.__setitem__(0, 5) if where == "stream" else None
    )
    requests = []

    def source(request):
        requests.append(request)
        return httpx.Response(200, headers=metadata(total=1, pages=1), stream=stream)

    with pytest.raises(RuntimeError, match="^source scan failed$"):
        fetch(monkeypatch, source, _bounds=bounds)
    if where == "before-request":
        assert requests == []
    else:
        assert stream.closed

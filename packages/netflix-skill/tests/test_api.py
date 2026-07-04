import httpx
import json


def make_client(handler):
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.example.test",
        headers={"authorization": "Bearer t"},
    )


def test_ensure_def_finds_existing_by_marker(ni):
    defs = [
        {"id": "other", "name": "Watched", "description": "", "annotation_type": "duration"},
        {"id": "target", "name": "Watched",
         "description": ni.DEF_MARKER, "annotation_type": "duration"},
    ]
    calls = []
    def handler(req):
        calls.append((req.method, req.url.path))
        assert req.method == "GET"
        return httpx.Response(200, json=defs)
    with make_client(handler) as c:
        assert ni.ensure_watched_def(c) == "target"
    assert calls == [("GET", "/user/v1alpha1/annotation")]   # no create!


def test_ensure_def_creates_when_absent(ni):
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=[])
        assert req.method == "POST"
        body = json.loads(req.content)
        assert body["name"] == "Watched"
        assert body["description"] == ni.DEF_MARKER
        assert body["annotation_type"] == "duration"
        assert body["tags"] == []                    # required by API
        assert body["measurement_spec"]["measurement_type"] == "duration"
        return httpx.Response(200, json={"id": "new-def"})
    with make_client(handler) as c:
        assert ni.ensure_watched_def(c) == "new-def"


def test_ensure_def_ignores_marker_on_wrong_type(ni):
    # A moment-type def carrying the marker must NOT satisfy resolution —
    # the annotation_type guard is load-bearing, not decorative.
    defs = [
        {"id": "wrong-type", "name": "Watched",
         "description": ni.DEF_MARKER, "annotation_type": "moment"},
    ]
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=defs)
        assert req.method == "POST"
        return httpx.Response(200, json={"id": "created"})
    with make_client(handler) as c:
        assert ni.ensure_watched_def(c) == "created"


def test_post_batch_chunks_and_content_type(ni):
    seen = []
    def handler(req):
        assert req.url.path == "/ingest/v1/record/batch"
        assert req.headers["content-type"] == "application/x-jsonl"
        seen.append(len(req.content.split(b"\n")))
        return httpx.Response(200)
    recs = [{"i": i} for i in range(1201)]
    with make_client(handler) as c:
        posted = ni.post_batch(c, recs, chunk_size=500)
    assert posted == 1201
    assert seen == [500, 500, 201]


def test_post_batch_raises_partial_post_error_with_posted_so_far(ni):
    # 3 chunks (500, 500, 201); the SECOND chunk's request 500s. The first
    # chunk's 500 records already landed server-side by that point, so the
    # raised error must carry posted_so_far == 500 — not 0 — or the caller
    # has no way to report a truthful envelope for the records that did post.
    calls = []
    def handler(req):
        calls.append(1)
        if len(calls) == 2:
            return httpx.Response(500)
        return httpx.Response(200)
    recs = [{"i": i} for i in range(1201)]
    with make_client(handler) as c:
        try:
            ni.post_batch(c, recs, chunk_size=500)
            assert False, "expected PartialPostError"
        except ni.PartialPostError as e:
            assert e.posted_so_far == 500
            assert isinstance(e.cause, httpx.HTTPStatusError)
            assert e.__cause__ is e.cause

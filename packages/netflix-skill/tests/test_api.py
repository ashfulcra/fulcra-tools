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

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient
from fulcra_media.state import State
from tests.conftest import json_response


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def test_ensure_tag_returns_cached_id_without_hitting_api(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected request {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(tag_ids={"netflix": "cached-uuid"})
    assert client.ensure_tag("netflix", state) == "cached-uuid"
    assert state.tag_ids == {"netflix": "cached-uuid"}


def test_ensure_tag_looks_up_existing_then_caches(recording_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/user/v1alpha1/tag/name/netflix":
            return json_response(200, {"id": "server-uuid", "name": "netflix"})
        pytest.fail(f"unexpected {request.method} {request.url}")
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State()
    tag_id = client.ensure_tag("netflix", state)
    assert tag_id == "server-uuid"
    assert state.tag_ids["netflix"] == "server-uuid"


def test_ensure_tag_creates_when_missing(recording_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)
        if request.method == "POST" and request.url.path == "/user/v1alpha1/tag":
            return json_response(200, {"id": "new-uuid", "name": "netflix"})
        pytest.fail(f"unexpected {request.method} {request.url}")
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State()
    tag_id = client.ensure_tag("netflix", state)
    assert tag_id == "new-uuid"
    assert state.tag_ids["netflix"] == "new-uuid"


def test_ensure_definitions_creates_watched_and_listened(recording_transport):
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # Bootstrap will first ensure_tag the three default tags
        if request.method == "GET" and "/tag/name/" in request.url.path:
            return httpx.Response(404)
        if request.method == "POST" and request.url.path == "/user/v1alpha1/tag":
            import json as _json
            body = _json.loads(request.content)
            return json_response(200, {"id": f"tag-{body['name']}", "name": body["name"]})
        if request.method == "POST" and request.url.path == "/user/v1alpha1/annotation":
            import json as _json
            body = _json.loads(request.content)
            posted.append(body)
            kind = body["name"].lower()
            return json_response(200, {"id": f"def-{kind}", **body})
        pytest.fail(f"unexpected {request.method} {request.url}")

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State()
    client.ensure_definitions(state)

    assert state.watched_definition_id == "def-watched"
    assert state.listened_definition_id == "def-listened"
    # Both definitions are DurationAnnotation with the right default tags
    assert {d["annotation_type"] for d in posted} == {"duration"}
    watched = next(d for d in posted if d["name"] == "Watched")
    listened = next(d for d in posted if d["name"] == "Listened")
    assert "tag-media" in watched["tags"] and "tag-watched" in watched["tags"]
    assert "tag-media" in listened["tags"] and "tag-listened" in listened["tags"]


def test_ensure_definitions_skips_when_already_cached(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(
        watched_definition_id="x", listened_definition_id="y",
        activity_definition_id="a", read_definition_id="r",
        tag_ids={"media": "m", "watched": "w", "listened": "l",
                 "activity": "ac", "read": "re"},
    )
    client.ensure_definitions(state)
    assert state.watched_definition_id == "x"

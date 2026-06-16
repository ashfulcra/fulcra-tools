import urllib.error
from unittest.mock import MagicMock

import httpx
import pytest
from fulcra_common.client import BaseFulcraClient

from fulcra_media.fulcra import FulcraClient
from fulcra_media.state import State
from media_test_helpers import json_response


def _missing_tag_lib() -> MagicMock:
    """A fulcra_api lib stub where every tag lookup 404s and create_tag mints
    a deterministic `tag-<name>` id. Tag resolution moved onto the lib (see
    BaseFulcraClient._resolve_tag), so it no longer rides the httpx transport."""
    lib = MagicMock()
    lib.get_tag_by_name.side_effect = urllib.error.HTTPError(
        url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
    )
    lib.create_tag.side_effect = lambda name: [{"id": f"tag-{name}", "name": name}]
    return lib


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def test_ensure_tag_returns_cached_id_without_hitting_api(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected request {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(tag_ids={"netflix": "cached-uuid"})
    assert client.ensure_tag("netflix", state) == "cached-uuid"
    assert state.tag_ids == {"netflix": "cached-uuid"}


def test_ensure_tag_looks_up_existing_then_caches(recording_transport, mocker):
    fake_lib = MagicMock()
    fake_lib.get_tag_by_name.return_value = {"id": "server-uuid", "name": "netflix"}
    mocker.patch.object(BaseFulcraClient, "_lib", lambda self: fake_lib)
    # Tag resolution goes through the lib, never httpx — fail on any request.
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State()
    tag_id = client.ensure_tag("netflix", state)
    assert tag_id == "server-uuid"
    assert state.tag_ids["netflix"] == "server-uuid"


def test_ensure_tag_creates_when_missing(recording_transport, mocker):
    fake_lib = MagicMock()
    fake_lib.get_tag_by_name.side_effect = urllib.error.HTTPError(
        url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
    )
    fake_lib.create_tag.return_value = [{"id": "new-uuid", "name": "netflix"}]
    mocker.patch.object(BaseFulcraClient, "_lib", lambda self: fake_lib)
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State()
    tag_id = client.ensure_tag("netflix", state)
    assert tag_id == "new-uuid"
    assert state.tag_ids["netflix"] == "new-uuid"
    fake_lib.create_tag.assert_called_once_with("netflix")


def test_ensure_definitions_creates_watched_and_listened(recording_transport, mocker):
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # Tags bootstrap through the lib (mocked below); definitions still POST
        # over httpx.
        if request.method == "POST" and request.url.path == "/user/v1alpha1/annotation":
            import json as _json
            body = _json.loads(request.content)
            posted.append(body)
            kind = body["name"].lower()
            return json_response(200, {"id": f"def-{kind}", **body})
        pytest.fail(f"unexpected {request.method} {request.url}")

    # The three default tags are missing → created as `tag-<name>` via the lib.
    mocker.patch.object(BaseFulcraClient, "_lib", lambda self: _missing_tag_lib())
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
        read_definition_id="r",
        tag_ids={"media": "m", "watched": "w", "listened": "l",
                 "read": "re"},
    )
    client.ensure_definitions(state)
    assert state.watched_definition_id == "x"

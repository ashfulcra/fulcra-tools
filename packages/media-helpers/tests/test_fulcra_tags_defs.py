import urllib.error
from unittest.mock import MagicMock

import pytest
from fulcra_common.client import BaseFulcraClient

from fulcra_media.fulcra import FulcraClient
from fulcra_media.state import State


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-token")


def test_ensure_tag_returns_cached_id_without_hitting_api(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected request {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(tag_ids={"netflix": "cached-uuid"})
    assert client.ensure_tag("netflix", state) == "cached-uuid"
    assert state.tag_ids == {"netflix": "cached-uuid"}


def test_ensure_tag_looks_up_existing_then_caches(recording_transport, monkeypatch):
    fake_lib = MagicMock()
    fake_lib.get_tag_by_name.return_value = {"id": "server-uuid", "name": "netflix"}
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)
    # Tag resolution goes through the lib, never httpx — fail on any request.
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State()
    tag_id = client.ensure_tag("netflix", state)
    assert tag_id == "server-uuid"
    assert state.tag_ids["netflix"] == "server-uuid"


def test_ensure_tag_creates_when_missing(recording_transport, monkeypatch):
    fake_lib = MagicMock()
    fake_lib.get_tag_by_name.side_effect = urllib.error.HTTPError(
        url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
    )
    fake_lib.create_tag.return_value = [{"id": "new-uuid", "name": "netflix"}]
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State()
    tag_id = client.ensure_tag("netflix", state)
    assert tag_id == "new-uuid"
    assert state.tag_ids["netflix"] == "new-uuid"
    fake_lib.create_tag.assert_called_once_with("netflix")

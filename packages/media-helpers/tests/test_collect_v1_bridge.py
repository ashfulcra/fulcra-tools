"""The retained media compatibility facade must never use CLI auth in Collect mode."""
from types import SimpleNamespace

import pytest

from fulcra_common.client import ImportResult
from fulcra_media.fulcra import FulcraClient
from fulcra_media.state import State


class Writer:
    def __init__(self):
        self.calls = []
        self.tags = []
        def resolve_tag(name, *, allow_create=True):
            self.tags.append((name, allow_create))
            return "synthetic-tag"
        self.session = SimpleNamespace(resolve_tag=resolve_tag,
            client=SimpleNamespace(get_token=lambda: "captured-collect-token"))

    def run_import(self, events, **kwargs):
        self.calls.append((events, kwargs))
        return ImportResult(len(events), 0, len(events), len(events))


def test_collect_facade_uses_captured_writer_and_preserves_legacy_state(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "must-not-use-cli-or-env")
    writer = Writer()
    ctx = SimpleNamespace(_media_record_writer=writer, config={})
    state = State(listened_definition_id="legacy-definition", tag_ids={"existing": "legacy-tag"})
    client = FulcraClient(collect_context=ctx)
    assert client.get_token() == "captured-collect-token"
    assert client.ensure_tag("synthetic", state) == "synthetic-tag"
    result = client.run_import(["synthetic-event"], state,
        claim=lambda _: pytest.fail("legacy claims must not suppress v1 records"))
    assert result.verified == 1
    assert writer.calls == [(["synthetic-event"], {"chunk_size": 500, "check_only": False})]
    assert state.listened_definition_id == "legacy-definition"
    assert state.tag_ids == {"existing": "legacy-tag"}


@pytest.mark.parametrize("method", ["ingest_batch", "ingest_batch_typed"])
def test_collect_webhook_and_typed_entrypoints_cannot_reach_legacy_ingest(method):
    writer = Writer()
    client = FulcraClient(collect_context=SimpleNamespace(_media_record_writer=writer, config={}))
    result = getattr(client, method)(["synthetic-event"], State())
    assert result.verified == 1
    assert len(writer.calls) == 1


def test_collect_facade_requires_a_guarded_run_and_cannot_create_legacy_definitions():
    with pytest.raises(RuntimeError, match="guarded"):
        FulcraClient(collect_context=SimpleNamespace())
    client = FulcraClient(collect_context=SimpleNamespace(_media_record_writer=Writer(), config={}))
    with pytest.raises(RuntimeError, match="v1"):
        client.ensure_definitions(State())


def test_collect_preview_cannot_be_overridden_by_a_nonpreview_call():
    writer = Writer()
    client = FulcraClient(collect_context=SimpleNamespace(_media_record_writer=writer, config={"dry_run": True}))
    client.run_import(["synthetic-event"], State(), check_only=False)
    assert writer.calls[0][1]["check_only"] is True
    client.ensure_tag("synthetic", State())
    assert writer.tags == [("synthetic", False)]


@pytest.mark.parametrize("preview_key", [None, "dry_run", "preview_only"])
@pytest.mark.parametrize("method", ["run_import", "ingest_batch", "ingest_batch_typed"])
def test_bridge_forces_each_preview_mode_and_ignores_legacy_callbacks(monkeypatch, preview_key, method):
    from fulcra_common import BaseFulcraClient
    def bomb(*args, **kwargs):
        pytest.fail("legacy auth, transport or callback used")
    for name in ("get_token", "_client", "_lib"):
        monkeypatch.setattr(BaseFulcraClient, name, bomb)
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "unrelated-cli-token")
    writer = Writer()
    captured_http = object()
    writer.session.client._client = lambda: captured_http
    ctx = SimpleNamespace(_media_record_writer=writer, config={preview_key: True} if preview_key else {})
    client = FulcraClient(collect_context=ctx)
    state = State(listened_definition_id="legacy", tag_ids={"existing": "legacy"})
    events = ["synthetic-event"]
    kwargs = dict(chunk_size=7, check_only=False, claim=bomb, unclaim=bomb,
                  updates_summary=bomb, sleep_fn=bomb) if method == "run_import" else {}
    result = getattr(client, method)(events, state, **kwargs)
    assert result.verified == 1
    assert writer.calls == [(events, {"chunk_size": 7 if method == "run_import" else 500, "check_only": preview_key is not None})]
    assert client._client() is captured_http
    assert client.get_token() == "captured-collect-token"
    with pytest.raises(RuntimeError, match="unavailable"):
        client._lib()
    assert state == State(listened_definition_id="legacy", tag_ids={"existing": "legacy"})


@pytest.mark.parametrize("method", ["get_token", "_client", "run_import"])
def test_invalid_captured_writer_never_falls_back_to_cli_auth(monkeypatch, method):
    from fulcra_common import BaseFulcraClient
    def bomb(*args, **kwargs):
        pytest.fail("invalid captured runtime fell back to CLI")
    for name in ("get_token", "_client", "_lib"):
        monkeypatch.setattr(BaseFulcraClient, name, bomb)
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "must-not-use")
    client = FulcraClient(collect_context=SimpleNamespace(_media_record_writer=object(), config={}))
    with pytest.raises(AttributeError):
        getattr(client, method)(*[[], State()] if method == "run_import" else [])

"""Actual Collect media entrypoints must reach guarded v1 record transport."""
from __future__ import annotations

from contextlib import nullcontext
import base64
import importlib
import io
import json
import logging
import re
from types import SimpleNamespace

import httpx
import pytest

from fulcra_collect import config, credentials, db, state, worker
from fulcra_collect.plugin import RunContext
from fulcra_collect.v1_session import CollectRecordSession
from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC
from fulcra_media import collect_v1
from fulcra_media.state import State
from fulcra_media.v1 import media_spec

MODULES = ("lastfm", "deezer", "trakt", "netflix", "spotify_extended", "youtube", "apple_takeout",
    "apple_music_takeout", "generic_rss", "letterboxd", "goodreads", "apple_podcasts",
    "apple_podcasts_timemachine", "apple_tv", "generic_csv", "media_webhook", "spotify_ifttt")
ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
DESTINATION = "Event/11111111-2222-4333-8444-555555555555"
CONTROL = "Event/aaaaaaaa-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def isolated_wiring(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
        lambda *_: pytest.fail("wiring test attempted real network access"))
    monkeypatch.setattr(worker, "_make_fulcra_definition_client",
        lambda: pytest.fail("v1 producer attempted legacy definition resolution"))


@pytest.mark.parametrize("module", MODULES)
def test_every_media_plugin_requires_collect_sign_in_before_fetching(module, tmp_path, monkeypatch):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    monkeypatch.setattr(RunContext, "collect_token", lambda _: None)
    plugin = importlib.import_module("fulcra_media.plugins." + module).PLUGIN
    cfg = config.load()
    cfg.enabled.add(plugin.id)
    config.save(cfg)
    ctx = RunContext(plugin_id=plugin.id, config={}, config_epoch=cfg.plugin_epochs.get(plugin.id, ""),
        credentials={}, state=state.PluginState(plugin.id), log=logging.getLogger("synthetic"), _emit=lambda _: None)
    with pytest.raises(ValueError, match="Reconnect to Fulcra in Collect"):
        plugin.run(ctx)
    specs = plugin.record_data_types({"category": "watched"})
    assert specs and all(spec.base_type == "Event" for spec in specs.values())
    ctx._record_outbox_get = lambda key, default: {"groups": [{"pending": True}]}
    assert plugin.record_pending(ctx) is True


@pytest.mark.parametrize("mode", ["visible", "delayed", "preview", "invalid", "page-cap", "entry-cap"])
def test_real_lastfm_worker_uses_v1_and_resumes_uncertain_visibility(tmp_path, monkeypatch, mode):
    delay, preview = mode == "delayed", mode == "preview"
    invalid = mode == "invalid"
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    from fulcra_media.plugins import lastfm
    cfg = config.load()
    cfg.enabled.add("lastfm")
    cfg.update_plugin_settings("lastfm", {"username": "synthetic-user", "preview_only": preview,
        "v1_destination_ids": {"listened": DESTINATION},
        **({"max_pages": 2} if mode == "page-cap" else {"max_entries": 2} if mode == "entry-cap" else {})})
    config.save(cfg)
    if mode in ("page-cap", "entry-cap"):
        from tomlkit.items import Integer
        field = "max_pages" if mode == "page-cap" else "max_entries"
        assert type(config.load().plugin_settings["lastfm"][field]) is Integer
    # A readonly preview uses the binding captured by destination setup.
    CreationJournal(tmp_path / "v1-type-creations.sqlite").resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
    legacy = state.load("lastfm")
    legacy.watermark = "2020-01-01T00:00:00Z"
    legacy.definition_id = "old-selected-definition"
    state.save(legacy)
    claims = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": ACCOUNT,
        "exp": 4102444800}).encode()).decode().rstrip("=")
    token = "synthetic." + claims + ".signature"
    monkeypatch.setattr(credentials, "get_user_secret_fresh", lambda _: token)
    monkeypatch.setattr(credentials, "get_secret", lambda *_: "synthetic-key")
    monkeypatch.setattr(lastfm, "_state_load", lambda _: State(listened_definition_id="old-selected-definition"))
    monkeypatch.setattr(lastfm, "_state_save", lambda _: pytest.fail("must preserve legacy media state"), raising=False)
    fetched = []
    actual_fetch = lastfm.fetch_recent_tracks
    def source(_request):
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps({"recenttracks": {
            "track": [{"name": "Synthetic track", "artist": {"#text": "Synthetic artist"},
                       "date": {"uts": "1767312000"}}], "@attr": {"totalPages": "1", "page": "1"}}}).encode()))
    def fetch(creds, **kwargs):
        fetched.append(kwargs["since"])
        return actual_fetch(creds, transport=httpx.MockTransport(source), **kwargs)
    monkeypatch.setattr(lastfm, "fetch_recent_tracks", fetch)
    actual_normalize = lastfm.normalize_history
    def normalize(raw, **kwargs):
        from dataclasses import replace
        events = list(actual_normalize(raw, **kwargs))
        return [replace(e, external_ids={**e.external_ids, "invalid": float("nan")}) for e in events] if invalid else events
    monkeypatch.setattr(lastfm, "normalize_history", normalize)
    schema = {"type": "object", "properties": {**media_spec("listened").fields,
        "id": {"type": "string", "format": "uuid"}, "sources": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string", "format": "uuid"}},
        "start_time": {"type": "string", "format": "date-time"}, "end_time": {"type": "string", "format": "date-time"}}}
    posted, rows, paths = [], {}, []
    visible = not delay
    def handler(request):
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer " + token
        if request.url.path == "/input/v1/data_type":
            return httpx.Response(200, json=[{"id": DESTINATION, "fulcra_userid": ACCOUNT, "name": "Listened",
                "api_version": "v1", "recordable": True, "deprecated": False, "record_spec": {"type": "event"}},
                {"id": CONTROL, "fulcra_userid": ACCOUNT, "name": EXCLUSION_SPEC.name,
                 "api_version": "v1", "recordable": True, "deprecated": False, "record_spec": {"type": "event"}}])
        if request.url.path.endswith("/schema"):
            if CONTROL in request.url.path:
                return httpx.Response(200, json={"type": "object", "properties": {
                    **{k: {} for k in ("id", "sources", "tags", "start_time", "end_time")}, **EXCLUSION_SPEC.fields},
                    "required": list(EXCLUSION_SPEC.required_fields)})
            return httpx.Response(200, json=schema)
        if request.url.path.startswith(("/user/v1alpha1/tag/name/", "/user/v1alpha1/tag/id/")):
            return httpx.Response(200, json={"id": "aaaaaaaa-bbbb-4ccc-8ddd-123456789abc",
                "name": "lastfm", "fulcra_userid": ACCOUNT})
        if request.url.path == "/data/v1/records":
            assert request.url.params["fulcra_userid"] == ACCOUNT
            identifiers = re.findall(r'id="([^"]+)"', request.url.params["q"])
            return httpx.Response(200, text="\n".join(json.dumps(rows[i]) for i in identifiers if visible and i in rows))
        if request.url.path == "/ingest/v1/record/" + DESTINATION:
            assert request.url.params["api_version"] == "v1"
            posted.extend(json.loads(line) for line in request.content.decode().splitlines())
            rows.update({row["id"]: row for row in posted})
            return httpx.Response(201, json={"upload_id": "synthetic"})
        raise AssertionError("unexpected Fulcra path: " + request.url.path)
    monkeypatch.setattr(collect_v1, "CollectRecordSession", lambda ctx: CollectRecordSession(
        ctx, transport=httpx.MockTransport(handler)))
    output = io.StringIO()
    assert worker.run_plugin(lastfm.PLUGIN, out=output) == ("error" if delay or invalid else "done"), output.getvalue()
    assert len(posted) == (0 if preview or invalid else 1)
    if posted:
        assert posted[0]["title"] == "Synthetic track"
    assert state.load("lastfm").watermark == "2020-01-01T00:00:00Z"
    result = json.loads(output.getvalue().splitlines()[-1])
    assert result["watermark"] == "2020-01-01T00:00:00Z"
    if preview or invalid:
        assert not any(json.loads(line).get("type") == "annotation" for line in output.getvalue().splitlines())
        assert db.get_record_outbox(db.open(), "lastfm", "v1_media_pending") is None
        assert not (tmp_path / "v1-media-twins.sqlite").exists()
        if invalid:
            assert not posted  # Selected rows fail before record delivery.
    elif delay:
        assert not any(json.loads(line).get("type") == "annotation" for line in output.getvalue().splitlines())
        assert db.get_record_outbox(db.open(), "lastfm", "v1_media_pending")["groups"]
        visible = True
        assert worker.run_plugin(lastfm.PLUGIN, out=io.StringIO()) == "done"
        assert len(posted) == 1  # Exact readback resumes without a duplicate POST.
    else:
        assert not db.get_record_outbox(db.open(), "lastfm", "v1_media_pending")["groups"]
    if mode in ("page-cap", "entry-cap"):
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        assert any(e.get("cursor_state") == "withheld" and e.get("cursor_reason") == mode for e in events)
    assert fetched[0] is None  # Legacy cursor cannot skip never-converted history.
    assert "/user/v1alpha1/annotation" not in paths


@pytest.mark.parametrize("fail", [False, True])
def test_loopback_service_prepares_owned_v1_destination_and_closes_server(tmp_path, monkeypatch, fail):
    from fulcra_media.plugins import media_webhook
    from fulcra_media.webhook_receiver import ReceiverContext
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    cfg = config.load()
    cfg.enable("media-webhook")
    cfg.update_plugin_settings("media-webhook", {"v1_destination_ids": {"watched": DESTINATION}})
    config.save(cfg)
    monkeypatch.setattr(credentials, "get_secret", lambda *_: None)
    monkeypatch.setattr(media_webhook, "_state_load", lambda _: State(watched_definition_id="old-retained-definition"))
    events = []
    class Session:
        account_id = ACCOUNT
        def __init__(self, ctx):
            self.client = SimpleNamespace(get_token=lambda: "synthetic-collect-token")
        def __enter__(self):
            return self
        def __exit__(self, *_):
            events.append("session-closed")
        def commit(self, callback):
            return callback()
        def record_write_scope(self):
            return nullcontext()
        def prepare_record_policy(self, *, check_only=False):
            assert not check_only
            events.append("policy-proved")
            return CONTROL
        def resolve_destination(self, spec, *, pinned_id, allow_create):
            assert spec.name == "Watched" and pinned_id == DESTINATION and not allow_create
            events.append("destination-proved")
            return SimpleNamespace(data_type=DESTINATION, schema={"properties": spec.fields})
    monkeypatch.setattr(collect_v1, "CollectRecordSession", Session)
    def make_server(**kwargs):
        assert kwargs["bearer_token"] is None
        health = ReceiverContext(client=kwargs["client"], state=kwargs["state"], bearer_token=None, host="127.0.0.1").health()
        assert health["data_type_id"] == DESTINATION and health["api_version"] == "v1"
        assert "definition_id" not in health
        assert events == ["destination-proved", "policy-proved"]
        class Server:
            def serve_forever(self):
                events.append("listening")
                if fail:
                    raise RuntimeError("synthetic receiver failure")
            def server_close(self):
                events.append("server-closed")
        return Server()
    monkeypatch.setattr(media_webhook.webhook_receiver, "make_server", make_server)
    result = worker.run_plugin(media_webhook.PLUGIN, out=io.StringIO())
    assert result == ("error" if fail else "done")
    assert events == ["destination-proved", "policy-proved", "listening", "server-closed", "session-closed"]

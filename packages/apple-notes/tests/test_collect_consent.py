"""Collect account/consent must bind every Notes Files and source operation."""
import base64
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from fulcra_collect import config
from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState
from fulcra_collect.v1_session import CollectRecordSession
from fulcra_apple_notes import collect_plugin, vaultio
from fulcra_apple_notes.collect_io import CollectNotesFiles, NotesConsent

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
VERSION = "11111111-2222-4333-8444-555555555555"
REMOTE = "/vault/notes/apple/synthetic.md"


def token(account=ACCOUNT, nonce=0):
    claims = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": account,
        "exp": 4102444800, "nonce": nonce}).encode()).decode().rstrip("=")
    return "synthetic." + claims + ".signature"


def context(tmp_path, monkeypatch, settings=None):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    cfg = config.load()
    cfg.enable("apple-notes")
    cfg.update_plugin_settings("apple-notes", settings or {})
    config.save(cfg)
    cfg = config.load()
    ctx = RunContext(plugin_id="apple-notes", config=cfg.plugin_settings.get("apple-notes", {}),
        config_epoch=cfg.plugin_epochs["apple-notes"], credentials={}, state=PluginState("apple-notes"),
        log=logging.getLogger("synthetic"), _emit=lambda _: None)
    auth = [token()]
    monkeypatch.setattr(ctx, "collect_token", lambda: auth[0])
    return ctx, auth


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *_: pytest.fail("unexpected real network"))
    monkeypatch.setattr(vaultio, "_run", lambda *a, **kw: pytest.fail("Collect inherited CLI authentication"))
    monkeypatch.setattr(collect_plugin.report, "write", lambda *a, **kw: None)


@pytest.mark.parametrize("mode", ["sync", "reconcile", "writeback"])
def test_missing_collect_signin_blocks_source_and_cli_before_every_mode(tmp_path, monkeypatch, mode):
    ctx, auth = context(tmp_path, monkeypatch, {"mode": mode, "writeback_enabled": True, "dry_run": False})
    auth[0] = None
    monkeypatch.setattr(collect_plugin, "run_sync", lambda **kw: pytest.fail("source touched without Collect signin"))
    monkeypatch.setattr(collect_plugin, "run_reconcile", lambda **kw: pytest.fail("source touched without Collect signin"))
    with pytest.raises(ValueError, match="Reconnect to Fulcra in Collect"):
        collect_plugin.run(ctx)


def test_local_preview_needs_no_fulcra_auth_but_stops_when_selection_changes(tmp_path, monkeypatch):
    ctx, auth = context(tmp_path, monkeypatch, {"dry_run": True})
    auth[0] = None
    consent = NotesConsent(ctx)
    consent.guard()
    cfg = config.load()
    cfg.plugin_settings["apple-notes"]["container"] = "changed-synthetic-source"
    config.save(cfg)
    with pytest.raises(RuntimeError, match="changed"):
        consent.guard()


@pytest.mark.parametrize("change", ["account", "settings", "disable"])
def test_bound_upload_checks_consent_after_metadata_before_storage(tmp_path, monkeypatch, change):
    ctx, auth = context(tmp_path, monkeypatch)
    api, storage = [], []
    def handler(request):
        api.append(request)
        assert request.headers["Authorization"] == "Bearer " + token()
        changed = config.load()
        if change == "account":
            auth[0] = token(OTHER)
        elif change == "settings":
            changed.plugin_settings["apple-notes"]["container"] = "changed-synthetic-source"
            monkeypatch.setattr(config, "load", lambda: changed)
        else:
            changed.enabled.remove("apple-notes")
            monkeypatch.setattr(config, "load", lambda: changed)
        return httpx.Response(201, json={"file": {"id": VERSION}, "url": "https://storage.example/upload"})
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        with CollectNotesFiles(session, storage_transport=httpx.MockTransport(lambda r: storage.append(r) or httpx.Response(200))) as files:
            with pytest.raises(RuntimeError, match="changed"):
                files.write_text(REMOTE, "Synthetic note")
    assert len(api) == 1 and not storage


def test_bound_io_uses_current_collect_principal_and_no_auth_on_signed_storage(tmp_path, monkeypatch):
    ctx, auth = context(tmp_path, monkeypatch)
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", token(OTHER))
    api, storage = [], []
    def handler(request):
        api.append(request)
        assert request.headers["Authorization"] == "Bearer " + auth[0]
        if request.method == "POST":
            return httpx.Response(201, json={"file": {"id": VERSION}, "url": "https://storage.example/upload"})
        if request.url.path.endswith("/download"):
            return httpx.Response(302, headers={"Location": "https://storage.example/download"})
        return httpx.Response(200, json={"files": [{"id": VERSION, "name": "synthetic.md",
            "path": "/vault/notes/apple", "state": "uploaded", "fulcra_userid": ACCOUNT}]})
    def object_store(request):
        storage.append(request)
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=b"Synthetic note")
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        with CollectNotesFiles(session, storage_transport=httpx.MockTransport(object_store)) as files:
            assert files.read_text(REMOTE) == "Synthetic note"
            auth[0] = token(nonce=1)  # Same-account silent refresh.
            files.write_text(REMOTE, "Synthetic note")
            assert files.stat(REMOTE) is not None
    assert len(api) == 4 and len(storage) == 2


@pytest.mark.parametrize("body", [{}, {"files": [], "has_more": True}, {"files": [{"id": VERSION}]}])
def test_unknown_or_partial_listing_never_becomes_missing_state(tmp_path, monkeypatch, body):
    ctx, _ = context(tmp_path, monkeypatch)
    with CollectRecordSession(ctx, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as session:
        with CollectNotesFiles(session) as files:
            with pytest.raises(vaultio.VaultIOError) as failure:
                files.read_text(REMOTE)
            assert not isinstance(failure.value, vaultio.MissingFile)


def test_positive_complete_empty_listing_is_missing(tmp_path, monkeypatch):
    ctx, _ = context(tmp_path, monkeypatch)
    with CollectRecordSession(ctx, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"files": []}))) as session:
        with CollectNotesFiles(session) as files:
            assert files.stat(REMOTE) is None
            with pytest.raises(vaultio.MissingFile):
                files.read_text(REMOTE)


@pytest.mark.parametrize("revoke", [None, "account", "settings", "disable"])
def test_real_notes_sync_never_uses_cli_and_stops_after_revocation(notes_db, tmp_path, monkeypatch, revoke):
    from pathlib import PurePosixPath
    from uuid import uuid4
    ctx, auth = context(tmp_path / "collect", monkeypatch, {"container": str(notes_db), "dry_run": False})
    metadata, content, by_path = {}, {}, {}
    def handler(request):
        assert request.headers["Authorization"] == "Bearer " + auth[0]
        assert request.url.path.startswith("/input/v1/file_upload")
        if request.method == "POST":
            spec = json.loads(request.content)
            identifier = str(uuid4())
            metadata[identifier] = spec
            return httpx.Response(201, json={"file": {"id": identifier},
                "url": "https://storage.example/upload/" + identifier})
        if request.url.path.endswith("/download"):
            identifier = request.url.path.split("/")[-2]
            return httpx.Response(302, headers={"location": "https://storage.example/download/" + identifier})
        remote = str(PurePosixPath(request.url.params["path"]) / request.url.params["name"])
        identifier = by_path.get(remote)
        rows = [] if identifier is None else [{"id": identifier, "path": metadata[identifier]["path"],
            "name": metadata[identifier]["name"], "state": "uploaded", "fulcra_userid": ACCOUNT}]
        return httpx.Response(200, json={"files": rows})
    def object_store(request):
        assert "Authorization" not in request.headers
        identifier = request.url.path.split("/")[-1]
        if request.method == "GET":
            return httpx.Response(200, content=content[identifier])
        content[identifier] = request.content
        spec = metadata[identifier]
        by_path[str(PurePosixPath(spec["path"]) / spec["name"])] = identifier
        if revoke:
            changed = config.load()
            if revoke == "account":
                auth[0] = token(OTHER)
            elif revoke == "settings":
                changed.plugin_settings["apple-notes"]["container"] = "changed-source"
                monkeypatch.setattr(config, "load", lambda: changed)
            else:
                changed.enabled.remove("apple-notes")
                monkeypatch.setattr(config, "load", lambda: changed)
        return httpx.Response(200)
    monkeypatch.setattr(collect_plugin, "CollectRecordSession", lambda ctx: CollectRecordSession(ctx, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(collect_plugin, "CollectNotesFiles", lambda session: CollectNotesFiles(session, storage_transport=httpx.MockTransport(object_store)))
    if revoke:
        with pytest.raises(RuntimeError, match="changed"):
            collect_plugin.run(ctx)
        assert len(content) == 1
        assert "/vault/notes/apple/.sync-state.json" not in by_path
        assert "/vault/notes/apple/Apple Notes.md" not in by_path
    else:
        collect_plugin.run(ctx)
        state_id = by_path["/vault/notes/apple/.sync-state.json"]
        saved = json.loads(content[state_id])
        assert len(saved["notes"]) == 2 and len(saved["attachments"]) == 1
        assert "/vault/notes/apple/Apple Notes.md" in by_path
        prior = dict(by_path)
        collect_plugin.run(ctx)
        # State checkpoints may acquire a new version; unchanged note and
        # attachment paths/content are not uploaded again.
        for path, identifier in prior.items():
            if path != "/vault/notes/apple/.sync-state.json":
                assert by_path[path] == identifier


def test_preview_revocation_after_notes_read_prevents_attachment_read(notes_db, tmp_path, monkeypatch):
    from fulcra_apple_notes import notestore
    ctx, auth = context(tmp_path / "collect", monkeypatch, {"container": str(notes_db), "dry_run": True})
    auth[0] = None
    original = notestore.NoteStore.notes
    def notes(store):
        result = original(store)
        cfg = config.load()
        cfg.enabled.remove("apple-notes")
        config.save(cfg)
        return result
    monkeypatch.setattr(notestore.NoteStore, "notes", notes)
    monkeypatch.setattr(notestore.NoteStore, "attachments", lambda _: pytest.fail("source read after preview revoked"))
    with pytest.raises(RuntimeError, match="changed"):
        collect_plugin.run(ctx)


def test_revoked_writeback_optin_is_checked_at_the_actual_native_mutation(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from unittest.mock import Mock
    from fulcra_apple_notes import writeback
    ctx, _ = context(tmp_path, monkeypatch, {"mode": "writeback", "writeback_enabled": True, "dry_run": False})
    monkeypatch.setattr(collect_plugin, "CollectNotesFiles", lambda _: nullcontext(SimpleNamespace(namespace=ACCOUNT)))
    monkeypatch.setattr(collect_plugin, "run_reconcile", lambda **kw: {"complete": True,
        "changes": [{"uuid": "synthetic-note", "status": "vault_edited"}]})
    monkeypatch.setattr(collect_plugin, "load_state", lambda **kw: {})
    monkeypatch.setattr(collect_plugin, "_snapshot_for", lambda *a, **kw: ([], []))
    monkeypatch.setattr(writeback, "probe", lambda: (True, "synthetic available"))
    monkeypatch.setattr(writeback, "plan_writeback", lambda changes, **kw: ([(changes[0], SimpleNamespace(pk=1))], []))
    def revalidate(*a, **kw):
        cfg = config.load()
        cfg.plugin_settings["apple-notes"]["writeback_enabled"] = False
        config.save(cfg)
        return "Synthetic body"
    monkeypatch.setattr(collect_plugin, "_revalidated_body_for", revalidate)
    native = Mock()
    monkeypatch.setattr(writeback, "write_note_body", native)
    with pytest.raises(RuntimeError, match="changed"):
        collect_plugin.run(ctx)
    native.assert_not_called()


def test_reconciliation_checkpoints_are_partitioned_by_destination_account(tmp_path):
    from fulcra_apple_notes.reconcile_checkpoint import scope_key
    baseline = {"notes": {"synthetic": {"hash": "same"}}, "attachments": {}}
    assert scope_key(tmp_path, baseline, namespace=ACCOUNT) != scope_key(tmp_path, baseline, namespace=OTHER)
    assert scope_key(tmp_path, baseline) != scope_key(tmp_path, baseline, namespace=ACCOUNT)


def test_attachment_mime_uses_content_as_the_previous_cli_did(tmp_path, monkeypatch):
    ctx, _ = context(tmp_path / "collect", monkeypatch)
    source = tmp_path / "misleading.txt"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"synthetic")
    types, stored = [], []
    def api(request):
        types.append(json.loads(request.content)["content_type"])
        return httpx.Response(201, json={"file": {"id": VERSION}, "url": "https://storage.example/upload"})
    with CollectRecordSession(ctx, transport=httpx.MockTransport(api)) as session:
        with CollectNotesFiles(session, storage_transport=httpx.MockTransport(lambda r: stored.append(r.content) or httpx.Response(200))) as files:
            files.upload_file(str(source), REMOTE)
    assert types == ["image/png"]
    assert stored == [source.read_bytes()]


@pytest.mark.parametrize("status,headers", [(206, {}), (200, {"X-Next-Cursor": "next"}),
                                            (200, {"X-Total-Count": "1"})])
def test_real_notes_sync_refuses_partial_state_without_upload_or_checkpoint(notes_db, tmp_path, monkeypatch, status, headers):
    from fulcra_collect.files_io import FilesIOError
    ctx, _ = context(tmp_path / "collect", monkeypatch, {"container": str(notes_db), "dry_run": False})
    calls = []
    def handle(request):
        calls.append(request.method)
        return httpx.Response(status, json={"files": []}, headers=headers)
    monkeypatch.setattr(collect_plugin, "CollectRecordSession", lambda ctx: CollectRecordSession(ctx, transport=httpx.MockTransport(handle)))
    with pytest.raises(FilesIOError):
        collect_plugin.run(ctx)
    assert calls and set(calls) == {"GET"}

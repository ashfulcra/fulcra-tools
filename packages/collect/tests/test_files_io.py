"""Shared Files transport validates the whole namespace before serving rows."""
import base64
import json
import logging

import httpx
import pytest

from fulcra_collect import config
from fulcra_collect.files_io import CollectFiles, FilesIOError
from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState
from fulcra_collect.v1_session import CollectRecordSession

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
VERSION = "11111111-2222-4333-8444-555555555555"


def session_for(tmp_path, monkeypatch, handler):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    cfg = config.load()
    cfg.enable("gmail")
    config.save(cfg)
    cfg = config.load()
    ctx = RunContext(plugin_id="gmail", config={}, config_epoch=cfg.plugin_epochs["gmail"],
        credentials={}, state=PluginState("gmail"), log=logging.getLogger("synthetic"), _emit=lambda _: None)
    claims = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": ACCOUNT, "exp": 4102444800}).encode()).decode().rstrip("=")
    monkeypatch.setattr(ctx, "collect_token", lambda: "synthetic." + claims + ".signature")
    return CollectRecordSession(ctx, transport=httpx.MockTransport(handler))


def row(**over):
    return {"id": VERSION, "path": "/team/synthetic/task", "name": "one.md", "size": 3,
            "uploaded_at": "2026-10-23T12:00:00Z", "state": "uploaded", **over}


def test_shared_files_lists_complete_namespace_and_deletes_exact_version(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"files": [row()], "folders": ["archive"]})
    with session_for(tmp_path, monkeypatch, handler) as session, CollectFiles(session) as files:
        assert files.list_dir("/team/synthetic/task/") == [
            {"name": "archive", "is_dir": True},
            {"name": "one.md", "is_dir": False, "size": 3, "mtime": "2026-10-23T12:00:00Z"}]
        files.delete("/team/synthetic/task/one.md")
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == f"/input/v1/file_upload/{VERSION}"


@pytest.mark.parametrize("body", [
    {"files": [], "folders": [], "has_more": True},
    {"files": [], "folders": "unreadable"},
    {"files": [row(path="/different")], "folders": []},
    {"files": [row(name="../other")], "folders": []},
    {"files": [row(), row()], "folders": []},
    {"files": [row(size=True)], "folders": []},
    {"files": [], "folders": ["../escape"]},
])
def test_listing_unknown_never_returns_partial_or_empty(tmp_path, monkeypatch, body):
    with session_for(tmp_path, monkeypatch, lambda _: httpx.Response(200, json=body)) as session, CollectFiles(session) as files:
        with pytest.raises(FilesIOError):
            files.list_dir("/team/synthetic/task/")


def test_explicit_null_folders_is_the_api_empty_folder_set(tmp_path, monkeypatch):
    with session_for(tmp_path, monkeypatch, lambda _: httpx.Response(200, json={"files": [], "folders": None})) as session, CollectFiles(session) as files:
        assert files.list_dir("/team/synthetic/task/") == []


@pytest.mark.parametrize("status,headers", [
    (206, {}), (200, {"Content-Range": "items 0-0/2"}),
    (200, {"X-Next-Cursor": "synthetic-next"}), (200, {"Link": "<https://example.test>; rel=next"}),
    (200, {"X-Total-Count": "1"}), (200, {"X-Total": "-1"}),
    (200, {"X-Total-Count": "unreadable"}), (200, {"X-Total": "0, 0"}),
])
@pytest.mark.parametrize("operation", ["list_dir", "stat", "delete"])
def test_partial_http_never_proves_files_absence(tmp_path, monkeypatch, status, headers, operation):
    calls = []
    def handle(request):
        calls.append(request.method)
        return httpx.Response(status, json={"files": []}, headers=headers)
    with session_for(tmp_path, monkeypatch, handle) as session, CollectFiles(session) as files:
        with pytest.raises(FilesIOError) as error:
            getattr(files, operation)("/team/synthetic/task/one.md")
        from fulcra_collect.files_io import MissingFile
        assert not isinstance(error.value, MissingFile)
    assert calls == ["GET"]


@pytest.mark.parametrize("storage", [False, True])
@pytest.mark.parametrize("status,headers", [(206, {}), (200, {"Content-Range": "bytes 0-2/9"}),
                                            (200, {"X-Next-Cursor": "next"})])
def test_partial_download_never_returns_utf8_prefix(tmp_path, monkeypatch, storage, status, headers):
    def handle(request):
        if request.url.path.endswith("/download"):
            return (httpx.Response(302, headers={"location": "https://storage.example/read"}) if storage
                    else httpx.Response(status, content=b"pre", headers=headers))
        return httpx.Response(200, json={"files": [row()]})
    with session_for(tmp_path, monkeypatch, handle) as session, CollectFiles(session,
            storage_transport=httpx.MockTransport(lambda _: httpx.Response(status, content=b"pre", headers=headers))) as files:
        with pytest.raises(FilesIOError):
            files.read_text("/team/synthetic/task/one.md")


@pytest.mark.parametrize("storage", [False, True])
@pytest.mark.parametrize("size", [0, 4, -1, True, False, 3.0, "3", None])
def test_known_version_size_must_match_complete_download_before_text(tmp_path, monkeypatch, storage, size):
    calls = []
    def handle(request):
        calls.append(request.method)
        if request.url.path.endswith("/download"):
            return (httpx.Response(302, headers={"location": "https://storage.example/read"}) if storage
                    else httpx.Response(200, content=b"pre", headers={"Content-Length": "3"}))
        return httpx.Response(200, json={"files": [row(size=size)]})
    with session_for(tmp_path, monkeypatch, handle) as session, CollectFiles(session,
            storage_transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"pre", headers={"Content-Length": "3"}))) as files:
        with pytest.raises(FilesIOError):
            files.read_text("/team/synthetic/task/one.md")
    assert set(calls) == {"GET"}


@pytest.mark.parametrize("storage", [False, True])
@pytest.mark.parametrize("text", ["", "full", "Unicode café ☃"])
def test_matching_version_size_uses_utf8_bytes_including_empty_files(tmp_path, monkeypatch, storage, text):
    body = text.encode("utf-8")
    def handle(request):
        if request.url.path.endswith("/download"):
            return (httpx.Response(302, headers={"location": "https://storage.example/read"}) if storage
                    else httpx.Response(200, content=body))
        return httpx.Response(200, json={"files": [row(size=len(body))]})
    with session_for(tmp_path, monkeypatch, handle) as session, CollectFiles(session,
            storage_transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as files:
        assert files.read_text("/team/synthetic/task/one.md") == text


@pytest.mark.parametrize("storage", [False, True])
def test_actual_note_merge_cannot_accept_a_known_size_short_200_suffix_loss(tmp_path, monkeypatch, storage):
    from fulcra_apple_notes.render import OPEN_FENCE, CLOSE_FENCE, merge_note
    prefix = "---\ntitle: Synthetic\n---\n" + OPEN_FENCE + "\nold\n" + CLOSE_FENCE
    suffix = "\nUser-maintained text outside the imported section.\n"
    complete = prefix + suffix
    def handle(request):
        if request.url.path.endswith("/download"):
            return (httpx.Response(302, headers={"location": "https://storage.example/read"}) if storage
                    else httpx.Response(200, content=prefix.encode()))
        return httpx.Response(200, json={"files": [row(size=len(complete.encode()))]})
    with session_for(tmp_path, monkeypatch, handle) as session, CollectFiles(session,
            storage_transport=httpx.MockTransport(lambda _: httpx.Response(200, content=prefix.encode()))) as files:
        try:
            downloaded = files.read_text("/team/synthetic/task/one.md")
        except FilesIOError:
            return
    assert suffix in merge_note(downloaded, prefix.replace("\nold\n", "\nnew\n"))


def test_complete_listing_totals_and_created_upload_remain_supported(tmp_path, monkeypatch):
    def handle(request):
        if request.method == "POST":
            return httpx.Response(201, json={"file": {"id": VERSION}, "url": "https://storage.example/upload"})
        return httpx.Response(200, json={"files": [row()]}, headers={"X-Total-Count": "1", "X-Total": "1"})
    uploaded = []
    with session_for(tmp_path, monkeypatch, handle) as session, CollectFiles(session,
            storage_transport=httpx.MockTransport(lambda r: uploaded.append(r.content) or httpx.Response(204))) as files:
        assert len(files.list_dir("/team/synthetic/task")) == 1
        files.write_text("/team/synthetic/task/one.md", "complete")
    assert uploaded == [b"complete"]


@pytest.mark.parametrize("status,headers", [(206, {}), (200, {"X-Next-Cursor": "next"}),
                                            (200, {"X-Total-Count": "1"})])
def test_real_coord_emitter_does_not_write_from_partial_files_absence(tmp_path, monkeypatch, status, headers):
    from fulcra_gmail.collect_relay import CollectCoordTransport
    from fulcra_gmail.relay import CoordEngineRelayEmitter, build_directive
    from fulcra_gmail.rules import parse_rules
    calls = []
    def handle(request):
        calls.append(request.method)
        return httpx.Response(status, json={"files": []}, headers=headers)
    with session_for(tmp_path, monkeypatch, handle) as session, CollectFiles(session) as files:
        transport = CollectCoordTransport(session, "synthetic", files=files)
        rule = parse_rules([{"id": "synthetic", "version": 1, "name": "Synthetic", "match": "x",
                             "actions": ["relay"], "relay_to": "recipient"}])[0]
        item = build_directive("relay-synthetic-only", rule)
        emitter = CoordEngineRelayEmitter("synthetic", run=transport.run, readback=transport.verify_directive)
        assert not emitter.emit(item).ok
    assert calls and set(calls) == {"GET"}

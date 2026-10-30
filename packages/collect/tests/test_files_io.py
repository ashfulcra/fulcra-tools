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

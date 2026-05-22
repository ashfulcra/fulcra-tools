"""fulcra-dayone CLI."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

import fulcra_dayone.cli as cli_mod
from fulcra_dayone.client import DayOneFulcraClient
from fulcra_dayone.cli import cli

_SAMPLE = {"entries": [
    {"uuid": "AAA111", "creationDate": "2024-01-15T09:30:00Z",
     "text": "First", "tags": ["work"], "starred": True},
    {"uuid": "BBB222", "creationDate": "2024-02-20T14:00:00Z",
     "text": "Second", "starred": False},
]}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-token")


def _export(tmp_path: Path) -> Path:
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "Personal.json").write_text(json.dumps(_SAMPLE), encoding="utf-8")
    return folder


def test_no_filters_without_all_is_an_error(tmp_path: Path):
    res = CliRunner().invoke(cli, ["import", str(_export(tmp_path))])
    assert res.exit_code != 0
    assert "--all" in res.output


def test_dry_run_reports_counts_without_network(tmp_path: Path):
    res = CliRunner().invoke(cli, ["import", str(_export(tmp_path)), "--all", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "Would import 2 entries" in res.output


def test_dry_run_with_starred_filter(tmp_path: Path):
    res = CliRunner().invoke(
        cli, ["import", str(_export(tmp_path)), "--starred", "--dry-run"],
    )
    assert res.exit_code == 0, res.output
    assert "Would import 1 entries" in res.output


def test_import_posts_to_fulcra(tmp_path: Path, monkeypatch):
    def responder(r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if r.method == "GET" and path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[])
        if r.method == "POST" and path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json={"id": "def-journal"})
        if r.method == "GET" and path.startswith("/user/v1alpha1/tag/name/"):
            return httpx.Response(200, json={"id": "tag-x"})
        if r.method == "GET" and path.startswith("/data/v1alpha1/event/"):
            return httpx.Response(200, json=[])
        if r.method == "POST" and path == "/ingest/v1/record/batch":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = httpx.MockTransport(responder)
    monkeypatch.setattr(
        cli_mod, "DayOneFulcraClient",
        lambda **kw: DayOneFulcraClient(transport=transport, **kw),
    )
    res = CliRunner().invoke(cli, ["import", str(_export(tmp_path)), "--all"])
    assert res.exit_code == 0, res.output
    assert "Imported" in res.output

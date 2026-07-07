"""CLI smoke tests — every command, client mocked."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
from click.testing import CliRunner

from fulcra_labs import cli
from labs_test_helpers import json_response, make_client

FIXTURES = Path(__file__).parent / "fixtures"


def test_markers_lists_registry():
    res = CliRunner().invoke(cli.cli, ["markers"])
    assert res.exit_code == 0
    assert "glucose" in res.output
    assert "marker(s)." in res.output


def test_markers_search_json():
    res = CliRunner().invoke(cli.cli, ["markers", "--search", "glucose", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    keys = {m["key"] for m in payload["markers"]}
    assert "glucose" in keys and "total-cholesterol" not in keys


def test_check_reports_disagreement():
    res = CliRunner().invoke(cli.cli, [
        "check", str(FIXTURES / "labcorp_pass_a.json"),
        str(FIXTURES / "labcorp_pass_b.json"), "--json",
    ])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["agreed_count"] == 9
    assert payload["disagreement_count"] == 1


def test_check_writes_agreed_out(tmp_path):
    out = tmp_path / "agreed.json"
    res = CliRunner().invoke(cli.cli, [
        "check", str(FIXTURES / "labcorp_pass_a.json"),
        str(FIXTURES / "labcorp_pass_b.json"), "--out", str(out),
    ])
    assert res.exit_code == 0
    agreed = json.loads(out.read_text())
    assert len(agreed["observations"]) == 9
    assert set(agreed) == {"lab", "report_date", "collected_at", "observations"}


def test_ingest_dry_run(monkeypatch):
    def boom(request):
        raise AssertionError("dry-run should not hit the network")
    client, _ = make_client(boom)
    monkeypatch.setattr(cli, "build_client", lambda: client)
    res = CliRunner().invoke(cli.cli, [
        "ingest", str(FIXTURES / "labcorp_pass_a.json"), "--dry-run",
    ])
    assert res.exit_code == 0, res.output
    assert "WOULD ingest 10/10" in res.output


def test_ingest_real_json(monkeypatch, tmp_path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/v1alpha1/annotation":
            return json_response(200, {"id": "def-x"})
        if request.url.path == "/ingest/v1/record":
            posted.append(json.loads(request.content))
            return httpx.Response(204)
        raise AssertionError(request.url)

    client, _ = make_client(handler)
    monkeypatch.setattr(cli, "build_client", lambda: client)
    res = CliRunner().invoke(cli.cli, [
        "ingest", str(FIXTURES / "labcorp_pass_a.json"), "--json",
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["outcome"]["ingested"] == 10
    assert len(posted) == 10


def test_status_empty_state(monkeypatch):
    monkeypatch.setattr(cli, "build_client", lambda: MagicMock())
    res = CliRunner().invoke(cli.cli, ["status", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["tracks_created"] == 0
    assert payload["registry_markers"] >= 45

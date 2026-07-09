"""CLI smoke tests — every command, client mocked."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
from click.testing import CliRunner

from fulcra_labs import cli, store
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
    """End-to-end typed ingest: a JSONL batch posts to
    /ingest/v1/record/NumericAnnotation and the landed-verification poll
    re-queries the event endpoint before the CLI reports success."""
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/user/v1alpha1/annotation" and method == "POST":
            return json_response(200, {"id": "def-x"})
        if path == "/ingest/v1/record/NumericAnnotation" and method == "POST":
            for line in request.content.decode().split("\n"):
                if line.strip():
                    posted.append(json.loads(line))
            return json_response(201, {"upload_id": "up-1"})
        if path == "/data/v1alpha1/event/NumericAnnotation" and method == "GET":
            return json_response(200, posted)   # everything lands
        raise AssertionError(request.url)

    monkeypatch.setattr(store, "_LANDING_POLL_SLEEP_S", 0)
    client, _ = make_client(handler)
    monkeypatch.setattr(cli, "build_client", lambda: client)
    res = CliRunner().invoke(cli.cli, [
        "ingest", str(FIXTURES / "labcorp_pass_a.json"), "--json",
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["outcome"]["ingested"] == 10
    assert len(posted) == 10
    assert all(r["unit"] and "value" in r for r in posted)   # first-class unit


def test_ingest_rerun_reports_already_present(monkeypatch):
    """Re-running the same report posts nothing (no server-side dedup on the
    typed endpoint — the pre-check is the only guard) and tells the operator
    distinctly from validation skips."""
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/user/v1alpha1/annotation" and method == "POST":
            return json_response(200, {"id": "def-x"})
        if path.startswith("/user/v1alpha1/annotation/") and method == "GET":
            return json_response(200, {"id": "def-x", "deleted_at": None})
        if path == "/ingest/v1/record/NumericAnnotation" and method == "POST":
            for line in request.content.decode().split("\n"):
                if line.strip():
                    posted.append(json.loads(line))
            return json_response(201, {"upload_id": "up-1"})
        if path == "/data/v1alpha1/event/NumericAnnotation" and method == "GET":
            return json_response(200, posted)
        raise AssertionError(request.url)

    monkeypatch.setattr(store, "_LANDING_POLL_SLEEP_S", 0)
    client, _ = make_client(handler)
    monkeypatch.setattr(cli, "build_client", lambda: client)
    args = ["ingest", str(FIXTURES / "labcorp_pass_a.json")]

    first = CliRunner().invoke(cli.cli, args)
    assert first.exit_code == 0, first.output
    assert len(posted) == 10

    second = CliRunner().invoke(cli.cli, args)
    assert second.exit_code == 0, second.output
    assert len(posted) == 10                    # zero new POSTs
    assert "ingested 0/10" in second.output
    assert "already in Fulcra (skipped): 10" in second.output


def test_status_empty_state(monkeypatch):
    monkeypatch.setattr(cli, "build_client", lambda: MagicMock())
    res = CliRunner().invoke(cli.cli, ["status", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["tracks_created"] == 0
    assert payload["registry_markers"] >= 45

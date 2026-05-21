"""End-to-end test for the `fulcra-csv export` CLI command.

Mocks the Fulcra endpoint via httpx.MockTransport so the test runs
hermetically — no real network calls."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from fulcra_csv import cli as cli_mod
from fulcra_csv.cli import cli
from fulcra_csv.fulcra import FulcraClient as RealFulcraClient


@pytest.fixture(autouse=True)
def _env_token(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")


def _record(start: str, note: str, value: float, def_id: str = "def-1") -> dict:
    return {
        "data": json.dumps({"note": note, "value": value, "unit": "kg"}),
        "recorded_at": {"start_time": start, "end_time": start},
        "sources": [
            f"com.fulcra.weight.v1.{note.replace(' ', '')}",
            f"com.fulcradynamics.annotation.{def_id}",
        ],
        "tag_names": ["weight"],
    }


def _mk_transport(records: list[dict]) -> httpx.MockTransport:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and "/data/v1alpha1/event/" in r.url.path:
            return httpx.Response(200, json=records)
        raise AssertionError(f"unexpected {r.method} {r.url}")
    return httpx.MockTransport(handler)


def test_export_writes_csv_to_stdout(tmp_path: Path, monkeypatch):
    records = [
        _record("2026-05-18T08:00:00Z", "morning", 80.4),
        _record("2026-05-19T08:00:00Z", "morning", 80.2),
    ]
    monkeypatch.setattr(
        cli_mod, "FulcraClient",
        lambda **kw: RealFulcraClient(transport=_mk_transport(records), **kw),
    )
    res = CliRunner().invoke(
        cli, ["export", "--data-type", "BodyMass",
              "--start", "2026-05-17T00:00:00Z",
              "--end",   "2026-05-20T00:00:00Z"],
    )
    assert res.exit_code == 0, res.output
    # First line is headers, then 2 data rows.
    lines = [line for line in res.output.splitlines() if line and not line.startswith("wrote ")]
    assert lines[0] == "start_time,end_time,tag,note,value"
    assert "morning" in lines[1]
    assert "80.4" in lines[1]
    assert "80.2" in lines[2]


def test_export_to_file(tmp_path: Path, monkeypatch):
    records = [_record("2026-05-18T08:00:00Z", "x", 1.0)]
    monkeypatch.setattr(
        cli_mod, "FulcraClient",
        lambda **kw: RealFulcraClient(transport=_mk_transport(records), **kw),
    )
    out = tmp_path / "export.csv"
    res = CliRunner().invoke(
        cli, ["export", "--data-type", "BodyMass",
              "--start", "2026-05-17T00:00:00Z",
              "--end",   "2026-05-20T00:00:00Z",
              "--out",   str(out)],
    )
    assert res.exit_code == 0, res.output
    assert out.exists()
    body = out.read_text()
    assert body.splitlines()[0] == "start_time,end_time,tag,note,value"
    # stderr-style status echoed
    assert "wrote 1 rows" in res.output


def test_export_filters_by_definition_id(monkeypatch):
    """When --definition-id is given, only records whose source array
    references that def should be exported."""
    records = [
        _record("2026-05-18T08:00:00Z", "keep", 1.0, def_id="want-uuid"),
        _record("2026-05-18T09:00:00Z", "drop", 2.0, def_id="other-uuid"),
    ]
    monkeypatch.setattr(
        cli_mod, "FulcraClient",
        lambda **kw: RealFulcraClient(transport=_mk_transport(records), **kw),
    )
    res = CliRunner().invoke(
        cli, ["export", "--definition-id", "want-uuid",
              "--start", "2026-05-17T00:00:00Z",
              "--end",   "2026-05-20T00:00:00Z"],
    )
    assert res.exit_code == 0, res.output
    assert "keep" in res.output
    assert "drop" not in res.output


def test_export_custom_columns(monkeypatch):
    records = [_record("2026-05-18T08:00:00Z", "n", 1.0)]
    monkeypatch.setattr(
        cli_mod, "FulcraClient",
        lambda **kw: RealFulcraClient(transport=_mk_transport(records), **kw),
    )
    res = CliRunner().invoke(
        cli, ["export", "--data-type", "BodyMass",
              "--start", "2026-05-17T00:00:00Z",
              "--end",   "2026-05-20T00:00:00Z",
              "--columns", "start_time,source_id,data.unit"],
    )
    assert res.exit_code == 0, res.output
    lines = [line for line in res.output.splitlines() if line and not line.startswith("wrote ")]
    assert lines[0] == "start_time,source_id,data.unit"
    # The non-def source id should land in the source_id column
    assert "com.fulcra.weight.v1." in lines[1]
    assert lines[1].endswith(",kg")


def test_export_requires_target():
    res = CliRunner().invoke(cli, ["export", "--start", "yesterday"])
    assert res.exit_code != 0
    assert "definition-id" in res.output or "data-type" in res.output


def test_export_rejects_inverted_range():
    res = CliRunner().invoke(
        cli, ["export", "--data-type", "BodyMass",
              "--start", "2026-05-20T00:00:00Z",
              "--end",   "2026-05-17T00:00:00Z"],
    )
    assert res.exit_code != 0
    assert "must be before" in res.output


def test_export_relative_start_parses(monkeypatch):
    """`dateparser` handles '1 week ago' — exercise the relative path."""
    monkeypatch.setattr(
        cli_mod, "FulcraClient",
        lambda **kw: __import__("fulcra_csv.fulcra", fromlist=["FulcraClient"]).FulcraClient(
            transport=_mk_transport([]), **kw),
    )
    res = CliRunner().invoke(
        cli, ["export", "--data-type", "BodyMass",
              "--start", "1 week ago",
              "--end",   "now"],
    )
    assert res.exit_code == 0, res.output

"""Integration tests for `fulcra-media import lastfm`.

Uses click.testing.CliRunner + monkeypatched fulcra-csv-importer's
FulcraClient + a mock Last.fm fetch so nothing hits the real network.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fulcra_media.cli import cli
from fulcra_media.fulcra import ImportResult
from fulcra_media.state import State


@pytest.fixture
def fake_state(tmp_path, monkeypatch):
    """Point the CLI at a tmp state.json with a Listened def already set."""
    state_path = tmp_path / "state.json"
    s = State(
        listened_definition_id="def-listened-uuid",
        watched_definition_id=None,
        tag_ids={"lastfm": "tag-lastfm-uuid"},
    )
    from fulcra_media import state as state_mod
    state_mod.save(s, state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)
    return state_path


@pytest.fixture
def lastfm_creds(tmp_path, monkeypatch):
    """Point load_creds() at a tmp creds file."""
    creds_path = tmp_path / "lastfm.json"
    creds_path.write_text(json.dumps({"username": "testuser", "api_key": "testkey"}))
    monkeypatch.setattr(
        "fulcra_media.importers.lastfm.CREDS_PATH", creds_path,
    )
    return creds_path


@pytest.fixture
def page1_tracks():
    return json.loads(
        (Path(__file__).parent / "fixtures" / "lastfm_recent_tracks_page1.json").read_text()
    )["recenttracks"]["track"]


def test_lastfm_cli_missing_creds_emits_error_envelope(fake_state, tmp_path, monkeypatch):
    """No creds file → ok=false, errors mentions auth."""
    monkeypatch.setattr(
        "fulcra_media.importers.lastfm.CREDS_PATH", tmp_path / "missing.json",
    )
    res = CliRunner().invoke(cli, ["import", "lastfm", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "auth"


def test_lastfm_cli_missing_definition_emits_error_envelope(
    tmp_path, monkeypatch, lastfm_creds,
):
    """No Listened def → ok=false, errors mentions setup."""
    state_path = tmp_path / "state.json"
    from fulcra_media import state as state_mod
    state_mod.save(State(), state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)

    res = CliRunner().invoke(cli, ["import", "lastfm", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "setup"


def test_lastfm_cli_cold_start_no_watermark_no_since(
    fake_state, lastfm_creds, page1_tracks, monkeypatch,
):
    """First run: no watermark, no --since → since stays None in envelope."""
    captured_since: list[datetime | None] = []
    def fake_fetch(creds, *, since=None, **kw):
        captured_since.append(since)
        yield from page1_tracks

    monkeypatch.setattr(
        "fulcra_media.importers.lastfm.fetch_recent_tracks", fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag", lambda self, name, state: "tag",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0, posted=len(events),
            verified=len(events),
        ),
    )

    res = CliRunner().invoke(cli, ["import", "lastfm", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["ok"] is True
    assert payload["importer"] == "lastfm"
    assert payload["since_watermark"] is None
    # nowplaying filtered → 4 events
    assert payload["posted"] == 4
    assert payload["new_watermark"] is not None
    # since was None on first call
    assert captured_since == [None]


def test_lastfm_cli_with_existing_watermark_uses_overlap(
    fake_state, lastfm_creds, page1_tracks, monkeypatch,
):
    """Stored watermark + --watermark-overlap-hours: fetch from watermark - overlap."""
    from fulcra_media import state as state_mod, watermarks
    s = state_mod.load(fake_state)
    watermarks.set_iso(s, "lastfm", datetime(2024, 5, 16, 23, 0, tzinfo=timezone.utc))
    state_mod.save(s, fake_state)

    captured_since: list[datetime | None] = []
    def fake_fetch(creds, *, since=None, **kw):
        captured_since.append(since)
        return iter(page1_tracks)
    monkeypatch.setattr(
        "fulcra_media.importers.lastfm.fetch_recent_tracks", fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag", lambda self, name, state: "tag",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0, posted=len(events),
            verified=len(events),
        ),
    )

    res = CliRunner().invoke(cli, ["import", "lastfm", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    # since should be watermark - 1 hour
    assert captured_since[0] == datetime(2024, 5, 16, 22, 0, tzinfo=timezone.utc)
    assert payload["since_watermark"] == "2024-05-16T22:00:00+00:00"


def test_lastfm_cli_check_only_does_not_post(
    fake_state, lastfm_creds, page1_tracks, monkeypatch,
):
    """--check-only sets would_post; doesn't write watermark."""
    monkeypatch.setattr(
        "fulcra_media.importers.lastfm.fetch_recent_tracks",
        lambda creds, **kw: iter(page1_tracks),
    )
    posted: list[bool] = []
    def fake_run(self, events, state, *, check_only=False, **kw):
        posted.append(check_only)
        return ImportResult(
            total=len(events), skipped_existing=0, posted=len(events) if check_only else 0,
            verified=0,
        )
    monkeypatch.setattr("fulcra_media.fulcra.FulcraClient.run_import", fake_run)

    res = CliRunner().invoke(cli, ["import", "lastfm", "--check-only", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["would_post"] == 4
    assert posted == [True]
    # No watermark update on check-only
    from fulcra_media import watermarks
    from fulcra_media import state as state_mod
    s2 = state_mod.load(fake_state)
    assert watermarks.get_iso(s2, "lastfm") is None


def test_lastfm_cli_explicit_since_overrides_watermark(
    fake_state, lastfm_creds, page1_tracks, monkeypatch,
):
    from fulcra_media import state as state_mod, watermarks
    s = state_mod.load(fake_state)
    watermarks.set_iso(s, "lastfm", datetime(2024, 5, 16, 23, 0, tzinfo=timezone.utc))
    state_mod.save(s, fake_state)

    captured_since: list[datetime | None] = []
    def fake_fetch(creds, *, since=None, **kw):
        captured_since.append(since)
        return iter(page1_tracks)
    monkeypatch.setattr(
        "fulcra_media.importers.lastfm.fetch_recent_tracks", fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag", lambda self, name, state: "tag",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0, posted=len(events), verified=0,
        ),
    )

    res = CliRunner().invoke(cli, [
        "import", "lastfm",
        "--since", "2020-01-01T00:00:00Z",
        "--json",
    ])
    assert res.exit_code == 0, res.output
    assert captured_since[0] == datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_lastfm_cli_fetch_error_surfaces_in_envelope(
    fake_state, lastfm_creds, monkeypatch,
):
    """A RuntimeError from fetch (e.g. last.fm rate limit) becomes errors[].stage=fetch."""
    def boom(creds, **kw):
        raise RuntimeError("Last.fm API error 29: Rate limit exceeded")
    monkeypatch.setattr(
        "fulcra_media.importers.lastfm.fetch_recent_tracks", boom,
    )
    res = CliRunner().invoke(cli, ["import", "lastfm", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "fetch"
    assert "Rate limit" in payload["errors"][0]["message"]


def test_lastfm_cli_invalid_since_format_emits_args_error(
    fake_state, lastfm_creds,
):
    res = CliRunner().invoke(cli, [
        "import", "lastfm", "--since", "not a date", "--json",
    ])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "args"

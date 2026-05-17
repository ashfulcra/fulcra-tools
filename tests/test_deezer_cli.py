"""Integration tests for `fulcra-media import deezer`."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from fulcra_media.cli import cli
from fulcra_media.fulcra import ImportResult
from fulcra_media.state import State


@pytest.fixture
def fake_state(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    s = State(
        listened_definition_id="def-listened-uuid",
        watched_definition_id=None,
        tag_ids={"deezer": "tag-deezer-uuid"},
    )
    from fulcra_media import state as state_mod
    state_mod.save(s, state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)
    return state_path


@pytest.fixture
def deezer_creds(tmp_path, monkeypatch):
    creds_path = tmp_path / "deezer.json"
    creds_path.write_text(json.dumps({"access_token": "testtok"}))
    monkeypatch.setattr(
        "fulcra_media.importers.deezer.CREDS_PATH", creds_path,
    )
    return creds_path


@pytest.fixture
def page1_tracks():
    return json.loads(
        (Path(__file__).parent / "fixtures" / "deezer_history_page1.json").read_text()
    )["data"]


def test_deezer_cli_missing_creds_emits_error_envelope(fake_state, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "fulcra_media.importers.deezer.CREDS_PATH", tmp_path / "missing.json",
    )
    res = CliRunner().invoke(cli, ["import", "deezer", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "auth"


def test_deezer_cli_missing_definition_emits_error_envelope(
    tmp_path, monkeypatch, deezer_creds,
):
    state_path = tmp_path / "state.json"
    from fulcra_media import state as state_mod
    state_mod.save(State(), state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)

    res = CliRunner().invoke(cli, ["import", "deezer", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "setup"


def test_deezer_cli_cold_start_no_watermark(
    fake_state, deezer_creds, page1_tracks, monkeypatch,
):
    """First run: no watermark, no --since → since stays None."""
    captured_since: list[datetime | None] = []

    def fake_fetch(creds, *, since=None, **kw):
        captured_since.append(since)
        yield from page1_tracks

    monkeypatch.setattr(
        "fulcra_media.importers.deezer.fetch_history", fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag",
        lambda self, name, state: "tag",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0, posted=len(events),
            verified=len(events),
        ),
    )

    res = CliRunner().invoke(cli, ["import", "deezer", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["ok"] is True
    assert payload["importer"] == "deezer"
    assert payload["since_watermark"] is None
    assert payload["posted"] == 4
    assert payload["new_watermark"] is not None
    assert captured_since == [None]


def test_deezer_cli_watermark_driven_incremental(
    fake_state, deezer_creds, page1_tracks, monkeypatch,
):
    """Stored watermark: fetch from (watermark - overlap)."""
    from fulcra_media import state as state_mod, watermarks
    s = state_mod.load(fake_state)
    watermarks.set_iso(s, "deezer", datetime(2024, 5, 16, 23, 0, tzinfo=timezone.utc))
    state_mod.save(s, fake_state)

    captured_since: list[datetime | None] = []

    def fake_fetch(creds, *, since=None, **kw):
        captured_since.append(since)
        return iter(page1_tracks)

    monkeypatch.setattr(
        "fulcra_media.importers.deezer.fetch_history", fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag",
        lambda self, name, state: "tag",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0, posted=len(events),
            verified=len(events),
        ),
    )

    res = CliRunner().invoke(cli, ["import", "deezer", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert captured_since[0] == datetime(2024, 5, 16, 22, 0, tzinfo=timezone.utc)
    assert payload["since_watermark"] == "2024-05-16T22:00:00+00:00"


def test_deezer_cli_check_only_does_not_post(
    fake_state, deezer_creds, page1_tracks, monkeypatch,
):
    monkeypatch.setattr(
        "fulcra_media.importers.deezer.fetch_history",
        lambda creds, **kw: iter(page1_tracks),
    )
    posted: list[bool] = []

    def fake_run(self, events, state, *, check_only=False, **kw):
        posted.append(check_only)
        return ImportResult(
            total=len(events), skipped_existing=0,
            posted=len(events) if check_only else 0,
            verified=0,
        )

    monkeypatch.setattr("fulcra_media.fulcra.FulcraClient.run_import", fake_run)

    res = CliRunner().invoke(cli, ["import", "deezer", "--check-only", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["would_post"] == 4
    assert posted == [True]
    # No watermark update on check-only
    from fulcra_media import watermarks
    from fulcra_media import state as state_mod
    s2 = state_mod.load(fake_state)
    assert watermarks.get_iso(s2, "deezer") is None


def test_deezer_cli_json_envelope_shape(
    fake_state, deezer_creds, page1_tracks, monkeypatch,
):
    monkeypatch.setattr(
        "fulcra_media.importers.deezer.fetch_history",
        lambda creds, **kw: iter(page1_tracks),
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag",
        lambda self, name, state: "tag",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0, posted=len(events),
            verified=len(events),
        ),
    )
    res = CliRunner().invoke(cli, ["import", "deezer", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output.strip())
    required = {
        "importer", "ok", "total", "skipped_existing", "posted", "verified",
        "since_watermark", "new_watermark", "would_post", "errors",
    }
    assert required <= set(payload.keys())
    # Should be exactly one JSON line
    lines = [ln for ln in res.output.split("\n") if ln.strip()]
    assert len(lines) == 1


def test_deezer_cli_fetch_error_surfaces_in_envelope(
    fake_state, deezer_creds, monkeypatch,
):
    def boom(creds, **kw):
        raise RuntimeError("Deezer API error 300: Invalid OAuth access token.")
    monkeypatch.setattr(
        "fulcra_media.importers.deezer.fetch_history", boom,
    )
    res = CliRunner().invoke(cli, ["import", "deezer", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "fetch"
    assert "Invalid OAuth" in payload["errors"][0]["message"]


def test_deezer_cli_invalid_since_format_emits_args_error(
    fake_state, deezer_creds,
):
    res = CliRunner().invoke(cli, [
        "import", "deezer", "--since", "not a date", "--json",
    ])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "args"


def test_deezer_cli_explicit_since_overrides_watermark(
    fake_state, deezer_creds, page1_tracks, monkeypatch,
):
    from fulcra_media import state as state_mod, watermarks
    s = state_mod.load(fake_state)
    watermarks.set_iso(s, "deezer", datetime(2024, 5, 16, 23, 0, tzinfo=timezone.utc))
    state_mod.save(s, fake_state)

    captured_since: list[datetime | None] = []

    def fake_fetch(creds, *, since=None, **kw):
        captured_since.append(since)
        return iter(page1_tracks)

    monkeypatch.setattr(
        "fulcra_media.importers.deezer.fetch_history", fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag",
        lambda self, name, state: "tag",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0, posted=len(events), verified=0,
        ),
    )

    res = CliRunner().invoke(cli, [
        "import", "deezer",
        "--since", "2020-01-01T00:00:00Z",
        "--json",
    ])
    assert res.exit_code == 0, res.output
    assert captured_since[0] == datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_deezer_wizard_prints_setup_steps():
    res = CliRunner().invoke(cli, ["wizard", "deezer"])
    assert res.exit_code == 0
    assert "deezer" in res.output.lower()
    # Must mention the manual mint URL and the creds path
    assert "developers.deezer.com" in res.output
    assert "~/.config/fulcra-media/deezer.json" in res.output
    assert "access_token" in res.output

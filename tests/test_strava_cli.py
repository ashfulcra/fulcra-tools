"""Integration tests for `fulcra-media import strava`."""
import json
import time
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
        listened_definition_id=None,
        watched_definition_id=None,
        activity_definition_id="def-activity-uuid",
        tag_ids={"strava": "tag-strava-uuid"},
    )
    from fulcra_media import state as state_mod
    state_mod.save(s, state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)
    return state_path


@pytest.fixture
def strava_creds(tmp_path, monkeypatch):
    creds_path = tmp_path / "strava.json"
    creds_path.write_text(json.dumps({
        "client_id": "cid", "client_secret": "csec",
        "access_token": "tok", "refresh_token": "rt",
        "expires_at": int(time.time()) + 10_000,
    }))
    monkeypatch.setattr(
        "fulcra_media.importers.strava.CREDS_PATH", creds_path,
    )
    return creds_path


@pytest.fixture
def page1_activities():
    return json.loads(
        (Path(__file__).parent / "fixtures" / "strava_activities_page1.json").read_text()
    )


def test_strava_cli_missing_creds_emits_error_envelope(fake_state, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "fulcra_media.importers.strava.CREDS_PATH", tmp_path / "missing.json",
    )
    res = CliRunner().invoke(cli, ["import", "strava", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "auth"


def test_strava_cli_missing_activity_definition_errors(
    tmp_path, monkeypatch, strava_creds,
):
    state_path = tmp_path / "state.json"
    from fulcra_media import state as state_mod
    state_mod.save(State(), state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)

    res = CliRunner().invoke(cli, ["import", "strava", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "setup"


def test_strava_cli_cold_start_no_watermark(
    fake_state, strava_creds, page1_activities, monkeypatch,
):
    """First run: no watermark, no --since → since stays None."""
    captured_since: list[datetime | None] = []

    def fake_fetch(creds, *, since=None, **kw):
        captured_since.append(since)
        yield from page1_activities

    monkeypatch.setattr(
        "fulcra_media.importers.strava.fetch_activities", fake_fetch,
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

    res = CliRunner().invoke(cli, ["import", "strava", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["ok"] is True
    assert payload["importer"] == "strava"
    assert payload["since_watermark"] is None
    assert payload["posted"] == 3
    assert payload["new_watermark"] is not None
    assert captured_since == [None]


def test_strava_cli_watermark_driven_uses_after_param(
    fake_state, strava_creds, page1_activities, monkeypatch,
):
    """Stored watermark: since=watermark (no overlap subtraction)."""
    from fulcra_media import state as state_mod, watermarks
    s = state_mod.load(fake_state)
    wm_dt = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    watermarks.set_iso(s, "strava", wm_dt)
    state_mod.save(s, fake_state)

    captured_since: list[datetime | None] = []

    def fake_fetch(creds, *, since=None, **kw):
        captured_since.append(since)
        return iter(page1_activities)

    monkeypatch.setattr(
        "fulcra_media.importers.strava.fetch_activities", fake_fetch,
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

    res = CliRunner().invoke(cli, ["import", "strava", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    # Strava's `after` is a strict GT so we hand it the watermark directly.
    assert captured_since[0] == wm_dt
    assert payload["since_watermark"] == "2026-05-01T00:00:00+00:00"


def test_strava_cli_check_only_does_not_post(
    fake_state, strava_creds, page1_activities, monkeypatch,
):
    monkeypatch.setattr(
        "fulcra_media.importers.strava.fetch_activities",
        lambda creds, **kw: iter(page1_activities),
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

    res = CliRunner().invoke(cli, ["import", "strava", "--check-only", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["would_post"] == 3
    assert posted == [True]
    # No watermark update on check-only
    from fulcra_media import watermarks
    from fulcra_media import state as state_mod
    s2 = state_mod.load(fake_state)
    assert watermarks.get_iso(s2, "strava") is None


def test_strava_cli_json_envelope_shape(
    fake_state, strava_creds, page1_activities, monkeypatch,
):
    monkeypatch.setattr(
        "fulcra_media.importers.strava.fetch_activities",
        lambda creds, **kw: iter(page1_activities),
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
    res = CliRunner().invoke(cli, ["import", "strava", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output.strip())
    required = {
        "importer", "ok", "total", "skipped_existing", "posted", "verified",
        "since_watermark", "new_watermark", "would_post", "errors",
    }
    assert required <= set(payload.keys())
    lines = [ln for ln in res.output.split("\n") if ln.strip()]
    assert len(lines) == 1


def test_strava_cli_fetch_error_surfaces_in_envelope(
    fake_state, strava_creds, monkeypatch,
):
    def boom(creds, **kw):
        raise RuntimeError("Strava API error: Authorization")
    monkeypatch.setattr(
        "fulcra_media.importers.strava.fetch_activities", boom,
    )
    res = CliRunner().invoke(cli, ["import", "strava", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "fetch"
    assert "Authorization" in payload["errors"][0]["message"]


def test_strava_cli_invalid_since_format_emits_args_error(
    fake_state, strava_creds,
):
    res = CliRunner().invoke(cli, [
        "import", "strava", "--since", "not a date", "--json",
    ])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "args"


def test_strava_cli_explicit_since_overrides_watermark(
    fake_state, strava_creds, page1_activities, monkeypatch,
):
    from fulcra_media import state as state_mod, watermarks
    s = state_mod.load(fake_state)
    watermarks.set_iso(s, "strava", datetime(2026, 4, 1, tzinfo=timezone.utc))
    state_mod.save(s, fake_state)

    captured_since: list[datetime | None] = []

    def fake_fetch(creds, *, since=None, **kw):
        captured_since.append(since)
        return iter(page1_activities)

    monkeypatch.setattr(
        "fulcra_media.importers.strava.fetch_activities", fake_fetch,
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
        "import", "strava",
        "--since", "2020-01-01T00:00:00Z",
        "--json",
    ])
    assert res.exit_code == 0, res.output
    assert captured_since[0] == datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_strava_cli_max_pages_passed_to_fetch(
    fake_state, strava_creds, page1_activities, monkeypatch,
):
    captured_kw: dict = {}
    def fake_fetch(creds, *, since=None, **kw):
        captured_kw.update(kw)
        return iter(page1_activities)
    monkeypatch.setattr(
        "fulcra_media.importers.strava.fetch_activities", fake_fetch,
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
    res = CliRunner().invoke(cli, ["import", "strava", "--max-pages", "2", "--json"])
    assert res.exit_code == 0, res.output
    assert captured_kw.get("max_pages") == 2


def test_strava_cli_uses_strava_auth_to_refresh_token(
    fake_state, strava_creds, page1_activities, monkeypatch,
):
    """CLI should construct a StravaAuth (which refresh_if_needed)
    and hand its creds dict to fetch_activities."""
    refreshed: list[bool] = []
    captured_creds: list[dict] = []

    class FakeAuth:
        def __init__(self):
            self.creds = {"access_token": "freshtoken"}
        def refresh_if_needed(self):
            refreshed.append(True)

    monkeypatch.setattr(
        "fulcra_media.importers.strava.StravaAuth", FakeAuth,
    )

    def fake_fetch(creds, *, since=None, **kw):
        captured_creds.append(creds)
        return iter(page1_activities)

    monkeypatch.setattr(
        "fulcra_media.importers.strava.fetch_activities", fake_fetch,
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
    res = CliRunner().invoke(cli, ["import", "strava", "--json"])
    assert res.exit_code == 0, res.output
    assert refreshed == [True]
    assert captured_creds[0]["access_token"] == "freshtoken"

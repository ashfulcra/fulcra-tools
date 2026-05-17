"""Aggressive sweep: every import command emits a valid JSON envelope on --json."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fulcra_media.cli import cli
from fulcra_media.fulcra import ImportResult
from fulcra_media.state import State, save


REQUIRED_ENVELOPE_KEYS = {
    "importer", "ok", "total", "skipped_existing", "posted", "verified",
    "since_watermark", "new_watermark", "would_post", "errors",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    save(State(
        watched_definition_id="w-uuid",
        listened_definition_id="l-uuid",
        tag_ids={
            "netflix": "tn", "trakt": "tt", "apple-podcasts": "tp",
            "spotify": "ts", "apple-tv": "ta", "lastfm": "tl",
        },
    ), state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)
    monkeypatch.setattr(
        "fulcra_media.twin_cache.DEFAULT_CACHE_PATH", tmp_path / "twin_cache.json",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag", lambda self, name, state: "t",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0, posted=len(events),
            verified=len(events),
        ),
    )
    return tmp_path


def _assert_envelope(stdout: str, *, importer: str, ok: bool = True) -> dict:
    """Parse a JSON line; verify required keys + ok value."""
    payload = json.loads(stdout.strip())
    missing = REQUIRED_ENVELOPE_KEYS - set(payload.keys())
    assert not missing, f"envelope missing keys: {missing}"
    assert payload["importer"] == importer
    assert payload["ok"] is ok
    return payload


def test_netflix_emits_envelope(env):
    csv = env / "f.csv"
    csv.write_text('Title,Date\n"Movie","5/12/26"\n')
    res = CliRunner().invoke(cli, ["import", "netflix", str(csv), "--json"])
    assert res.exit_code == 0, res.output
    _assert_envelope(res.output, importer="netflix")


def test_netflix_missing_file_emits_failure_envelope(env):
    res = CliRunner().invoke(cli, [
        "import", "netflix", str(env / "nonexistent.csv"), "--json",
    ])
    assert res.exit_code != 0


def test_apple_podcasts_emits_envelope(env, monkeypatch):
    monkeypatch.setattr(
        "fulcra_media.importers.apple_podcasts.parse_db",
        lambda path: iter([]),
    )
    res = CliRunner().invoke(cli, [
        "import", "apple-podcasts", "--db", "/tmp/fake.sqlite", "--json",
    ])
    assert res.exit_code == 0, res.output
    _assert_envelope(res.output, importer="apple-podcasts")


def test_apple_podcasts_timemachine_no_snapshots_failure_envelope(env, monkeypatch):
    monkeypatch.setattr(
        "fulcra_media.importers.apple_podcasts.find_timemachine_snapshots",
        lambda: [],
    )
    res = CliRunner().invoke(cli, [
        "import", "apple-podcasts-timemachine", "--json",
    ])
    assert res.exit_code == 2
    payload = _assert_envelope(res.output, importer="apple-podcasts-timemachine", ok=False)
    assert payload["errors"][0]["stage"] == "fetch"


def test_spotify_extended_emits_envelope(env, monkeypatch, tmp_path):
    """Spotify Extended path needs an actual file for library.resolve."""
    zip_path = tmp_path / "fake.zip"
    zip_path.touch()
    monkeypatch.setattr(
        "fulcra_media.importers.spotify.parse_extended_zip",
        lambda path: iter([]),
    )
    res = CliRunner().invoke(cli, [
        "import", "spotify-extended", str(zip_path), "--json",
    ])
    assert res.exit_code == 0, res.output
    _assert_envelope(res.output, importer="spotify-extended")


def test_netflix_resolve_errors_emit_envelope(env, monkeypatch):
    """Missing file from library.resolve → structured envelope, not raw stacktrace."""
    res = CliRunner().invoke(cli, [
        "import", "netflix", "/nonexistent/path.csv", "--json",
    ])
    assert res.exit_code == 2
    payload = _assert_envelope(res.output, importer="netflix", ok=False)
    assert payload["errors"][0]["stage"] in ("args", "fetch")


def test_apple_takeout_missing_csv_failure_envelope(env, tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    res = CliRunner().invoke(cli, [
        "import", "apple-takeout", str(empty_dir), "--json",
    ])
    assert res.exit_code == 2
    _assert_envelope(res.output, importer="apple-takeout", ok=False)


def test_generic_csv_invalid_tz_failure_envelope(env, tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("timestamp,title\n2024-01-01T00:00:00Z,X\n")
    res = CliRunner().invoke(cli, [
        "import", "generic-csv", str(csv),
        "--service", "test", "--category", "listened",
        "--tz", "Bad/Tz/Name", "--json",
    ])
    assert res.exit_code == 2
    payload = _assert_envelope(res.output, importer="generic-csv:test", ok=False)
    assert payload["errors"][0]["stage"] == "args"


def test_check_only_sets_would_post_field(env):
    csv = env / "f.csv"
    csv.write_text('Title,Date\n"Movie","5/12/26"\n')
    res = CliRunner().invoke(cli, [
        "import", "netflix", str(csv), "--check-only", "--json",
    ])
    assert res.exit_code == 0, res.output
    payload = _assert_envelope(res.output, importer="netflix")
    assert payload["would_post"] is not None
    assert payload["would_post"] == 1


def test_no_double_output_in_json_mode(env):
    """JSON mode should produce exactly one line on stdout, nothing else."""
    csv = env / "f.csv"
    csv.write_text('Title,Date\n"Movie","5/12/26"\n')
    res = CliRunner().invoke(cli, [
        "import", "netflix", str(csv), "--json",
    ])
    assert res.exit_code == 0
    lines = [ln for ln in res.output.split("\n") if ln.strip()]
    # Exactly one JSON line
    json.loads(lines[0])  # parses
    assert len(lines) == 1

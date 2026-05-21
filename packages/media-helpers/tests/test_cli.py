from pathlib import Path

from click.testing import CliRunner

from fulcra_media.cli import cli
from fulcra_media.state import State, save


def test_cli_no_args_shows_help():
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert "bootstrap" in result.output
    assert "wizard" in result.output
    assert "import" in result.output
    assert "status" in result.output


def test_status_prints_state_file(tmp_path: Path, mocker):
    state_path = tmp_path / "state.json"
    save(State(
        watched_definition_id="w",
        listened_definition_id="l",
        tag_ids={"netflix": "t"},
        watermarks={"netflix-slim": "2026-05-12"},
    ), state_path)
    mocker.patch("fulcra_media.state.DEFAULT_PATH", state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "watched_definition_id" in result.output
    assert "netflix" in result.output


def test_bootstrap_calls_ensure_definitions(tmp_path: Path, mocker):
    state_path = tmp_path / "state.json"
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    def fake_ensure(self, state):
        state.watched_definition_id = "w-id"
        state.listened_definition_id = "l-id"
        state.tag_ids["media"] = "m"

    mocker.patch("fulcra_media.fulcra.FulcraClient.ensure_definitions", fake_ensure)
    result = CliRunner().invoke(cli, ["bootstrap"])
    assert result.exit_code == 0, result.output
    # State persisted to disk
    persisted = State()
    import json as _json
    raw = _json.loads(state_path.read_text())
    assert raw["watched_definition_id"] == "w-id"
    assert raw["listened_definition_id"] == "l-id"


def test_wizard_netflix_invokes_walkthrough():
    result = CliRunner().invoke(cli, ["wizard", "netflix"], input="1\n")
    assert result.exit_code == 0
    assert "Download all" in result.output


def test_import_netflix_runs_pipeline(tmp_path: Path, mocker):
    # Prep a tiny CSV and state on disk
    csv = tmp_path / "small.csv"
    csv.write_text('Title,Date\n"Movie One","5/12/26"\n')
    state_path = tmp_path / "state.json"
    save(State(
        watched_definition_id="w", listened_definition_id="l",
        tag_ids={"netflix": "t"},
    ), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    # Stub the network-touching pipeline
    from fulcra_media.fulcra import ImportResult

    captured = {}
    def fake_run(self, events, state, chunk_size=500, window_pad_minutes=10, **kw):
        events = list(events)
        captured["count"] = len(events)
        return ImportResult(total=len(events), skipped_existing=0, posted=len(events), verified=len(events))
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import", fake_run)

    result = CliRunner().invoke(cli, ["import", "netflix", str(csv)])
    assert result.exit_code == 0, result.output
    assert captured["count"] == 1
    assert "posted=1" in result.output or "1 posted" in result.output


def test_import_netflix_resolves_fulcra_uri(tmp_path: Path, mocker):
    csv = tmp_path / "downloaded.csv"
    csv.write_text('Title,Date\n"Movie","5/12/26"\n')
    mocker.patch("fulcra_media.library.resolve", return_value=csv)

    state_path = tmp_path / "state.json"
    save(State(
        watched_definition_id="w", listened_definition_id="l",
        tag_ids={"netflix": "t"},
    ), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    from fulcra_media.fulcra import ImportResult
    mocker.patch(
        "fulcra_media.fulcra.FulcraClient.run_import",
        return_value=ImportResult(1, 0, 1, 1),
    )
    result = CliRunner().invoke(cli, ["import", "netflix", "fulcra:/takeouts/Netflix.csv"])
    assert result.exit_code == 0, result.output


def test_import_netflix_rich_variant(tmp_path: Path, mocker):
    """The CLI auto-detects the 10-column rich variant and routes correctly."""
    csv = tmp_path / "rich.csv"
    csv.write_text(
        'Profile Name,Start Time,Duration,Attributes,Title,Supplemental Video Type,Device Type,Bookmark,Latest Bookmark,Country\n'
        '"Ash","2026-05-12 20:00:00","00:30:00","","Some: Season 1: Ep","","Apple TV","00:30:00","00:30:00","US"\n'
    )
    state_path = tmp_path / "state.json"
    save(State(
        watched_definition_id="w", listened_definition_id="l",
        tag_ids={"netflix": "t"},
    ), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    captured = {}
    from fulcra_media.fulcra import ImportResult
    def fake_run(self, events, state, chunk_size=500, window_pad_minutes=10, **kw):
        events = list(events)
        captured["count"] = len(events)
        captured["importer"] = events[0].importer if events else None
        return ImportResult(total=len(events), skipped_existing=0, posted=len(events), verified=len(events))
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import", fake_run)

    result = CliRunner().invoke(cli, ["import", "netflix", str(csv)])
    assert result.exit_code == 0, result.output
    assert captured["count"] == 1
    assert captured["importer"] == "netflix-rich"


def test_import_trakt_runs_pipeline(tmp_path: Path, mocker):
    state_path = tmp_path / "state.json"
    save(State(watched_definition_id="w", listened_definition_id="l", tag_ids={"trakt": "t"}), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    from fulcra_media.importers.base import NormalizedEvent
    from datetime import datetime, timezone
    fake_events = [
        NormalizedEvent(
            importer="trakt", service="trakt", category="watched",
            note="X", title="X",
            start_time=datetime(2026,1,1,tzinfo=timezone.utc),
            end_time=datetime(2026,1,1,1,tzinfo=timezone.utc),
            deterministic_id="com.fulcra.media.trakt.v1.history.123",
            timestamp_confidence="high",
        ),
    ]
    mocker.patch("fulcra_media.importers.trakt.fetch_history", return_value=iter([]))
    mocker.patch("fulcra_media.importers.trakt.normalize_history", return_value=iter(fake_events))

    from fulcra_media.fulcra import ImportResult
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import",
                 return_value=ImportResult(1, 0, 1, 1))
    mocker.patch("fulcra_media.fulcra.FulcraClient.ensure_tag", return_value="t")

    result = CliRunner().invoke(cli, ["import", "trakt"])
    assert result.exit_code == 0, result.output
    assert "importer=trakt" in result.output


def test_import_apple_podcasts_runs_pipeline(tmp_path: Path, mocker):
    state_path = tmp_path / "state.json"
    save(State(watched_definition_id="w", listened_definition_id="l", tag_ids={"apple-podcasts": "t"}), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    from fulcra_media.importers.base import NormalizedEvent
    from datetime import datetime, timezone
    fake = [NormalizedEvent(
        importer="apple-podcasts", service="apple-podcasts", category="listened",
        note="x", title="x",
        start_time=datetime(2026,1,1,tzinfo=timezone.utc),
        end_time=datetime(2026,1,1,1,tzinfo=timezone.utc),
        deterministic_id="com.fulcra.media.apple-podcasts.v1.abc",
        timestamp_confidence="medium",
    )]
    mocker.patch("fulcra_media.importers.apple_podcasts.parse_db", return_value=iter(fake))
    from fulcra_media.fulcra import ImportResult
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import",
                 return_value=ImportResult(1, 0, 1, 1))
    mocker.patch("fulcra_media.fulcra.FulcraClient.ensure_tag", return_value="t")

    result = CliRunner().invoke(cli, ["import", "apple-podcasts", "--db", str(tmp_path / "fake.sqlite")])
    assert result.exit_code == 0, result.output
    assert "importer=apple-podcasts" in result.output


def test_import_apple_podcasts_timemachine_runs_pipeline(tmp_path: Path, mocker):
    state_path = tmp_path / "state.json"
    save(State(watched_definition_id="w", listened_definition_id="l", tag_ids={"apple-podcasts": "t"}), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    fake_snap = tmp_path / "snap.sqlite"
    fake_snap.write_text("")
    mocker.patch(
        "fulcra_media.importers.apple_podcasts.find_timemachine_snapshots",
        return_value=[fake_snap],
    )

    from fulcra_media.importers.base import NormalizedEvent
    from datetime import datetime, timezone
    fake = [NormalizedEvent(
        importer="apple-podcasts", service="apple-podcasts", category="listened",
        note="x", title="x",
        start_time=datetime(2026,1,1,tzinfo=timezone.utc),
        end_time=datetime(2026,1,1,1,tzinfo=timezone.utc),
        deterministic_id="com.fulcra.media.apple-podcasts.v1.abc",
        timestamp_confidence="medium",
    )]
    mocker.patch("fulcra_media.importers.apple_podcasts.parse_db", return_value=iter(fake))
    from fulcra_media.fulcra import ImportResult
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import",
                 return_value=ImportResult(1, 0, 1, 1))
    mocker.patch("fulcra_media.fulcra.FulcraClient.ensure_tag", return_value="t")

    result = CliRunner().invoke(cli, ["import", "apple-podcasts-timemachine"])
    assert result.exit_code == 0, result.output
    assert "importer=apple-podcasts-timemachine" in result.output


def test_import_apple_podcasts_timemachine_no_snapshots(tmp_path: Path, mocker):
    state_path = tmp_path / "state.json"
    save(State(watched_definition_id="w", listened_definition_id="l", tag_ids={"apple-podcasts": "t"}), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)
    mocker.patch(
        "fulcra_media.importers.apple_podcasts.find_timemachine_snapshots",
        return_value=[],
    )
    result = CliRunner().invoke(cli, ["import", "apple-podcasts-timemachine"])
    # Envelope-style failure → exit 2 (distinct from click's usage-error 1)
    assert result.exit_code == 2
    # Failure envelope is emitted; in human mode the error goes to stderr
    # (captured into result.output for CliRunner by default).
    assert "ok=False" in result.output


def test_import_spotify_extended_runs_pipeline(tmp_path: Path, mocker):
    state_path = tmp_path / "state.json"
    save(State(watched_definition_id="w", listened_definition_id="l", tag_ids={"spotify": "t"}), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    # Provide a fake zip path; the parser is mocked so the file just needs to exist
    fake_zip = tmp_path / "spotify.zip"
    fake_zip.write_bytes(b"")
    mocker.patch("fulcra_media.library.resolve", return_value=fake_zip)

    from fulcra_media.importers.base import NormalizedEvent
    from datetime import datetime, timezone
    fake = [NormalizedEvent(
        importer="spotify-extended", service="spotify", category="listened",
        note="x", title="x",
        start_time=datetime(2026,1,1,tzinfo=timezone.utc),
        end_time=datetime(2026,1,1,1,tzinfo=timezone.utc),
        deterministic_id="com.fulcra.media.spotify-extended.v1.abc",
        timestamp_confidence="high",
    )]
    mocker.patch("fulcra_media.importers.spotify.parse_extended_zip", return_value=iter(fake))
    from fulcra_media.fulcra import ImportResult
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import",
                 return_value=ImportResult(1, 0, 1, 1))
    mocker.patch("fulcra_media.fulcra.FulcraClient.ensure_tag", return_value="t")

    result = CliRunner().invoke(cli, ["import", "spotify-extended", str(fake_zip)])
    assert result.exit_code == 0, result.output
    assert "importer=spotify-extended" in result.output


def test_import_apple_takeout_runs_pipeline(tmp_path: Path, mocker):
    state_path = tmp_path / "state.json"
    save(State(watched_definition_id="w", listened_definition_id="l", tag_ids={"apple-tv": "t"}), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    fake_csv = tmp_path / "Playback Activity.csv"
    fake_csv.write_text("")
    mocker.patch("fulcra_media.library.resolve", return_value=fake_csv)

    from fulcra_media.importers.base import NormalizedEvent
    from datetime import datetime, timezone
    fake = [NormalizedEvent(
        importer="apple-takeout", service="apple-tv", category="watched",
        note="x", title="x",
        start_time=datetime(2026,1,1,tzinfo=timezone.utc),
        end_time=datetime(2026,1,1,1,tzinfo=timezone.utc),
        deterministic_id="com.fulcra.media.apple-takeout.v1.abc",
        timestamp_confidence="high",
    )]
    mocker.patch("fulcra_media.importers.apple_takeout.parse_playback_csv", return_value=iter(fake))
    from fulcra_media.fulcra import ImportResult
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import",
                 return_value=ImportResult(1, 0, 1, 1))
    mocker.patch("fulcra_media.fulcra.FulcraClient.ensure_tag", return_value="t")

    result = CliRunner().invoke(cli, ["import", "apple-takeout", str(fake_csv)])
    assert result.exit_code == 0, result.output
    assert "importer=apple-takeout" in result.output


def test_import_apple_takeout_finds_csv_in_directory(tmp_path: Path, mocker):
    """When given a directory, the CLI finds Playback Activity.csv inside."""
    state_path = tmp_path / "state.json"
    save(State(watched_definition_id="w", listened_definition_id="l", tag_ids={"apple-tv": "t"}), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    # Build an export-like dir tree
    export_dir = tmp_path / "apple_data_export"
    nested = export_dir / "Apple Media Services information" / "Apple TV"
    nested.mkdir(parents=True)
    csv_inside = nested / "Playback Activity.csv"
    csv_inside.write_text("")
    mocker.patch("fulcra_media.library.resolve", return_value=export_dir)

    captured = {}
    def fake_parse(path):
        captured["path"] = path
        return iter([])
    mocker.patch("fulcra_media.importers.apple_takeout.parse_playback_csv", side_effect=fake_parse)

    from fulcra_media.fulcra import ImportResult
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import",
                 return_value=ImportResult(0, 0, 0, 0))
    mocker.patch("fulcra_media.fulcra.FulcraClient.ensure_tag", return_value="t")

    result = CliRunner().invoke(cli, ["import", "apple-takeout", str(export_dir)])
    assert result.exit_code == 0, result.output
    assert captured["path"] == csv_inside

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
    def fake_run(self, events, state, chunk_size=500, window_pad_minutes=10):
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
    def fake_run(self, events, state, chunk_size=500, window_pad_minutes=10):
        events = list(events)
        captured["count"] = len(events)
        captured["importer"] = events[0].importer if events else None
        return ImportResult(total=len(events), skipped_existing=0, posted=len(events), verified=len(events))
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import", fake_run)

    result = CliRunner().invoke(cli, ["import", "netflix", str(csv)])
    assert result.exit_code == 0, result.output
    assert captured["count"] == 1
    assert captured["importer"] == "netflix-rich"

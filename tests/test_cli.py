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

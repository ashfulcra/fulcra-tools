"""Configuration is explicit and validated before either external system is used."""
import importlib.util
import json
from pathlib import Path

import pytest


HERE = Path(__file__).parent


@pytest.fixture
def bridge(monkeypatch):
    for key in ("ANSWERS_LINEAR_CONFIG", "ANSWERS_LINEAR_ENV", "LINEAR_API_KEY", "COORD_TEAM"):
        monkeypatch.delenv(key, raising=False)
    spec = importlib.util.spec_from_file_location("answers_config_test", HERE / "answers_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*args, **kwargs):
        raise AssertionError("configuration must finish before network or bus calls")

    monkeypatch.setattr(module.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    return module


@pytest.fixture
def config(tmp_path):
    data = {
        "project_id": "synthetic-project", "team_id": "synthetic-team",
        "states": {"open": "synthetic-open", "done": "synthetic-done"},
        "labels": {name: "synthetic-" + name for name in
                   ("qa-answer", "type:factual", "type:future-work", "type:both", "promote", "filed")},
        "sender": "operator", "workstream": "answer-followups",
    }
    path = tmp_path / "local.json"
    path.write_text(json.dumps(data))
    return path


def test_help_needs_no_config_or_credentials(bridge, capsys):
    with pytest.raises(SystemExit) as result:
        bridge.main(["--help"])
    assert result.value.code == 0
    assert "--config" in capsys.readouterr().out


def test_missing_config_is_actionable_without_loading_ambient_files(bridge, capsys):
    assert bridge.main(["list"]) == 2
    assert "ANSWERS_LINEAR_CONFIG" in capsys.readouterr().err


def test_missing_credentials_never_reaches_either_service(bridge, config, monkeypatch, capsys):
    monkeypatch.setenv("ANSWERS_LINEAR_CONFIG", str(config))
    monkeypatch.setenv("COORD_TEAM", "acme")
    assert bridge.main(["promote"]) == 2
    assert "LINEAR_API_KEY" in capsys.readouterr().err


def test_missing_team_never_guesses_a_namespace(bridge, config, monkeypatch, capsys):
    monkeypatch.setenv("LINEAR_API_KEY", "synthetic-key")
    assert bridge.main(["--config", str(config), "promote"]) == 2
    assert "COORD_TEAM" in capsys.readouterr().err


@pytest.mark.parametrize("contents", ["{bad-json", "[]", '{"project_id":"sensitive-value"}'])
def test_bad_configuration_reports_setup_error_without_echoing_content(
        bridge, tmp_path, contents, capsys):
    path = tmp_path / "private-config.json"
    path.write_text(contents)
    assert bridge.main(["--config", str(path), "list"]) == 2
    stderr = capsys.readouterr().err
    assert "configuration" in stderr.lower()
    assert "sensitive-value" not in stderr
    assert str(path) not in stderr


def test_config_flag_overrides_environment_and_preserves_configured_identity(
        bridge, config, monkeypatch):
    monkeypatch.setenv("ANSWERS_LINEAR_CONFIG", "missing-config.json")
    monkeypatch.setenv("LINEAR_API_KEY", "synthetic-key")
    monkeypatch.setenv("COORD_TEAM", "acme")
    seen = []

    def list_cards(args):
        seen.append((bridge.IDS["project_id"], bridge.TEAM, bridge.SENDER, bridge.WORKSTREAM))
        return 0

    monkeypatch.setattr(bridge, "cmd_list", list_cards)
    assert bridge.main(["--config", str(config), "list"]) == 0
    assert seen == [("synthetic-project", "acme", "operator", "answer-followups")]


def test_explicit_credential_file_and_environment_precedence(bridge, config, tmp_path, monkeypatch):
    env_file = tmp_path / "local.env"
    env_file.write_text('LINEAR_API_KEY="synthetic-file-key"\n')
    monkeypatch.setenv("ANSWERS_LINEAR_CONFIG", str(config))
    monkeypatch.setenv("ANSWERS_LINEAR_ENV", str(env_file))
    monkeypatch.setenv("COORD_TEAM", "acme")
    keys = []
    monkeypatch.setattr(bridge, "cmd_list", lambda args: keys.append(bridge.KEY) or 0)
    assert bridge.main(["list"]) == 0
    monkeypatch.setenv("LINEAR_API_KEY", "synthetic-env-key")
    assert bridge.main(["list"]) == 0
    assert keys == ["synthetic-file-key", "synthetic-env-key"]


def test_check_config_is_local_even_with_complete_setup(bridge, config, monkeypatch, capsys):
    monkeypatch.setenv("LINEAR_API_KEY", "synthetic-key")
    monkeypatch.setenv("COORD_TEAM", "acme")
    assert bridge.main(["--config", str(config), "check-config"]) == 0
    assert "no services contacted" in capsys.readouterr().out


def test_missing_explicit_files_do_not_fall_back_or_echo_paths(bridge, config, monkeypatch, capsys):
    assert bridge.main(["--config", "/nonexistent/private-config.json", "list"]) == 2
    monkeypatch.setenv("COORD_TEAM", "acme")
    monkeypatch.setenv("ANSWERS_LINEAR_ENV", "/nonexistent/private-credentials.env")
    assert bridge.main(["--config", str(config), "list"]) == 2
    stderr = capsys.readouterr().err
    assert "ANSWERS_LINEAR_ENV" in stderr
    assert "/nonexistent/" not in stderr


def test_empty_capture_content_is_rejected_before_setup(bridge):
    with pytest.raises(SystemExit) as result:
        bridge.main(["capture", "--q", "", "--a", "answer"])
    assert result.value.code == 2

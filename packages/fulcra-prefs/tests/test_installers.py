import json
from pathlib import Path

import pytest

from fulcra_prefs.cli import run
from fulcra_prefs.installers import install_platform_hooks


def _read_json(path: Path):
    return json.loads(path.read_text())


def test_install_claude_code_hooks_merges_settings_json(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{
                "hooks": [{"type": "command", "command": "user-start"}]
            }]
        }
    }))

    plan = install_platform_hooks(platform="claude-code", target_dir=tmp_path,
                                  cli="/opt/bin/fulcra-prefs")

    assert plan["config"] == str(settings)
    config = _read_json(settings)
    assert config["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "user-start"
    assert "fulcra-prefs-hooks/session-start.sh" in \
        config["hooks"]["SessionStart"][1]["hooks"][0]["command"]
    assert "fulcra-prefs-hooks/capture-candidates.sh" in \
        config["hooks"]["PreCompact"][0]["hooks"][0]["command"]
    assert "fulcra-prefs-hooks/capture-candidates.sh" in \
        config["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert (tmp_path / "fulcra-prefs-hooks/session-start.sh").exists()
    assert (tmp_path / "fulcra-prefs-hooks/capture-candidates.sh").exists()


def test_install_hooks_is_idempotent(tmp_path):
    install_platform_hooks(platform="codex", target_dir=tmp_path, cli="/prefs")
    install_platform_hooks(platform="codex", target_dir=tmp_path, cli="/prefs")

    config = _read_json(tmp_path / "hooks.json")
    assert len(config["hooks"]["SessionStart"]) == 1
    assert len(config["hooks"]["PreCompact"]) == 1
    assert "Stop" not in config["hooks"]


def test_install_removes_legacy_one_line_hook(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{
                "hooks": [{
                    "type": "command",
                    "command": "fulcra-prefs compile >/dev/null 2>&1; fulcra-prefs inject --platform claude-code",
                }]
            }]
        }
    }))

    install_platform_hooks(platform="claude-code", target_dir=tmp_path,
                           cli="/prefs")

    config = _read_json(settings)
    commands = [
        hook["command"]
        for entry in config["hooks"]["SessionStart"]
        for hook in entry["hooks"]
    ]
    assert commands == [str(tmp_path / "fulcra-prefs-hooks/session-start.sh")]


def test_uninstall_removes_only_managed_hooks(tmp_path):
    install_platform_hooks(platform="codex", target_dir=tmp_path, cli="/prefs")
    config = _read_json(tmp_path / "hooks.json")
    config["hooks"]["SessionStart"].insert(0, {
        "hooks": [{"type": "command", "command": "user-start"}]
    })
    (tmp_path / "hooks.json").write_text(json.dumps(config))

    install_platform_hooks(platform="codex", target_dir=tmp_path, uninstall=True)

    config = _read_json(tmp_path / "hooks.json")
    assert config["hooks"]["SessionStart"] == [{
        "hooks": [{"type": "command", "command": "user-start"}]
    }]
    assert "PreCompact" not in config["hooks"]


def test_invalid_json_refuses_without_replacing_user_config(tmp_path):
    bad = tmp_path / "hooks.json"
    bad.write_text("{not-json")

    with pytest.raises(ValueError, match="not valid JSON"):
        install_platform_hooks(platform="codex", target_dir=tmp_path,
                               cli="/prefs")

    assert bad.read_text() == "{not-json"
    assert not (tmp_path / "hooks.json.bak").exists()
    assert not (tmp_path / "fulcra-prefs-hooks").exists()


def test_cli_invalid_json_returns_error_without_replacing_user_config(
        fake_api, tmp_path, capsys):
    bad = tmp_path / "hooks.json"
    bad.write_text("{not-json")

    rc = run(["install-hooks", "--platform", "codex", "--target-dir", str(tmp_path)],
             api=fake_api, outbox_dir=tmp_path / "outbox", now=None)

    assert rc == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert bad.read_text() == "{not-json"
    assert not (tmp_path / "fulcra-prefs-hooks").exists()


def test_strip_managed_preserves_unknown_non_dict_entries(tmp_path):
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({
        "hooks": {
            "PreCompact": [
                "user-string-entry",
                {"hooks": [{"type": "command", "command": "/old/fulcra-prefs-hooks/capture-candidates.sh"}]},
            ]
        }
    }))

    install_platform_hooks(platform="codex", target_dir=tmp_path, cli="/prefs")

    config = _read_json(hooks)
    assert config["hooks"]["PreCompact"][0] == "user-string-entry"
    assert len(config["hooks"]["PreCompact"]) == 2


def test_dry_run_writes_nothing_and_returns_plan(tmp_path):
    plan = install_platform_hooks(platform="codex", target_dir=tmp_path,
                                  dry_run=True, cli="/prefs")

    assert json.loads(json.dumps(plan))["platform"] == "codex"
    assert not (tmp_path / "hooks.json").exists()
    assert not (tmp_path / "fulcra-prefs-hooks").exists()


def test_capture_hook_uses_session_candidate_file_and_marks_captured(tmp_path):
    install_platform_hooks(platform="codex", target_dir=tmp_path,
                           cli="/opt/bin/fulcra-prefs")
    script = (tmp_path / "fulcra-prefs-hooks/capture-candidates.sh").read_text()

    assert "$PLATFORM/$SID.json" in script
    assert "capture-batch --file" in script
    assert "--platform \"$PLATFORM\"" in script
    assert "--session \"$SID\"" in script
    assert "case \"$SID\"" in script
    assert ".captured" in script


def test_cli_install_hooks_dry_run(fake_api, tmp_path, capsys):
    rc = run(["install-hooks", "--platform", "codex", "--target-dir",
              str(tmp_path), "--dry-run"],
             api=fake_api, outbox_dir=tmp_path / "outbox",
             now=None)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["platform"] == "codex"
    assert out["config"] == str(tmp_path / "hooks.json")

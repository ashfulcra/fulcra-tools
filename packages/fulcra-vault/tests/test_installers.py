import json
import shutil
import subprocess

import pytest

from fulcra_vault.installers import install_platform_hooks


def _config(tmp_path, platform):
    name = "settings.json" if platform == "claude-code" else "hooks.json"
    return json.loads((tmp_path / name).read_text())


def test_install_merges_settings_json_preserving_user_hook(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "user-start"}]}]
        }
    }))

    install_platform_hooks(platform="claude-code", target_dir=tmp_path,
                           cli="/opt/bin/fulcra-vault")

    config = _config(tmp_path, "claude-code")
    starts = config["hooks"]["SessionStart"]
    assert starts[0]["hooks"][0]["command"] == "user-start"          # preserved
    assert "fulcra-vault-hooks" in starts[1]["hooks"][0]["command"]  # managed appended


def test_install_is_idempotent(tmp_path):
    install_platform_hooks(platform="codex", target_dir=tmp_path, cli="/v")
    install_platform_hooks(platform="codex", target_dir=tmp_path, cli="/v")

    config = _config(tmp_path, "codex")
    assert len(config["hooks"]["SessionStart"]) == 1


def test_uninstall_removes_only_managed_hooks(tmp_path):
    install_platform_hooks(platform="codex", target_dir=tmp_path, cli="/v")
    config = _config(tmp_path, "codex")
    config["hooks"]["SessionStart"].insert(
        0, {"hooks": [{"type": "command", "command": "user-start"}]})
    (tmp_path / "hooks.json").write_text(json.dumps(config))

    install_platform_hooks(platform="codex", target_dir=tmp_path, uninstall=True)

    config = _config(tmp_path, "codex")
    assert config["hooks"]["SessionStart"] == [
        {"hooks": [{"type": "command", "command": "user-start"}]}]


def test_invalid_json_refuses_without_replacing_user_config(tmp_path):
    (tmp_path / "hooks.json").write_text("{not json")

    with pytest.raises(ValueError, match="not valid JSON"):
        install_platform_hooks(platform="codex", target_dir=tmp_path, cli="/v")

    assert (tmp_path / "hooks.json").read_text() == "{not json"  # untouched


def test_dry_run_writes_nothing_and_returns_plan(tmp_path):
    plan = install_platform_hooks(platform="codex", target_dir=tmp_path,
                                  dry_run=True, cli="/v")

    assert plan["events"] == ["SessionStart"]
    assert not (tmp_path / "hooks.json").exists()
    assert not (tmp_path / "fulcra-vault-hooks").exists()


def test_unsupported_platform_raises(tmp_path):
    with pytest.raises(ValueError, match="unsupported platform"):
        install_platform_hooks(platform="emacs", target_dir=tmp_path)


def test_session_start_script_shape(tmp_path):
    install_platform_hooks(platform="codex", target_dir=tmp_path,
                           cli="/opt/bin/fulcra-vault")
    script = (tmp_path / "fulcra-vault-hooks/session-start.sh").read_text()

    assert "read HOT" in script
    assert "additionalContext" in script
    assert '[ -z "$HOT" ] && exit 0' in script      # fail-safe: empty -> no output
    assert "/opt/bin/fulcra-vault" in script


@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("python3")),
                    reason="needs bash and python3 to run the hook script")
def test_session_start_script_emits_hot_as_additional_context(tmp_path):
    stub = tmp_path / "fulcra-vault"
    stub.write_text('#!/usr/bin/env bash\n'
                    'if [ "$1" = "read" ] && [ "$2" = "HOT" ]; then '
                    'echo "# Hot"; echo "- hot note"; fi\n')
    stub.chmod(0o755)
    install_platform_hooks(platform="codex", target_dir=tmp_path, cli=str(stub))
    script = tmp_path / "fulcra-vault-hooks/session-start.sh"

    out = subprocess.run(["bash", str(script)], capture_output=True, text=True)

    data = json.loads(out.stdout)
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "hot note" in data["hookSpecificOutput"]["additionalContext"]


@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("python3")),
                    reason="needs bash and python3 to run the hook script")
def test_session_start_script_silent_when_hot_empty(tmp_path):
    stub = tmp_path / "fulcra-vault"
    stub.write_text('#!/usr/bin/env bash\nexit 0\n')  # prints nothing
    stub.chmod(0o755)
    install_platform_hooks(platform="codex", target_dir=tmp_path, cli=str(stub))
    script = tmp_path / "fulcra-vault-hooks/session-start.sh"

    out = subprocess.run(["bash", str(script)], capture_output=True, text=True)

    assert out.stdout.strip() == ""

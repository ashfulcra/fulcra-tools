"""Auth path: env override, shell-out, error surface."""
from __future__ import annotations

import subprocess

import pytest

from fulcra_attention.fulcra import FulcraClient


def test_env_override_takes_precedence(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-env-tok")
    client = FulcraClient()
    assert client.get_token() == "test-env-tok"


def test_shell_out_used_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, mocker
):
    """When FULCRA_ACCESS_TOKEN is unset, shell out to `fulcra auth print-access-token`.

    Prefer the venv-local `fulcra` binary (next to sys.executable) when it
    exists; fall back to PATH lookup otherwise. The launchd-managed relay
    inherits a minimal PATH that doesn't include the venv bin, so the
    sibling lookup is what keeps it working in production.
    """
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    fake = mocker.patch(
        "fulcra_attention.fulcra.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"shell-tok\n"
        ),
    )
    assert FulcraClient().get_token() == "shell-tok"
    # Either the sibling absolute path (venv test run) or bare "fulcra" (PATH).
    fake.assert_called_once()
    args, kwargs = fake.call_args
    cmd = args[0]
    assert len(cmd) == 3
    assert cmd[0].endswith("fulcra") or cmd[0] == "fulcra"
    assert cmd[1:] == ["auth", "print-access-token"]
    assert kwargs == {"check": True, "capture_output": True}


def test_shell_out_falls_back_to_path_when_sibling_missing(
    monkeypatch: pytest.MonkeyPatch, mocker, tmp_path
):
    """If `fulcra` isn't next to sys.executable, fall back to bare 'fulcra'
    so PATH-based discovery still works."""
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    # Point sys.executable at a directory with no `fulcra` next to it.
    fake_python = tmp_path / "python"
    fake_python.write_text("")
    mocker.patch("fulcra_attention.fulcra.sys.executable", str(fake_python))
    fake = mocker.patch(
        "fulcra_attention.fulcra.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"path-tok\n"
        ),
    )
    assert FulcraClient().get_token() == "path-tok"
    cmd = fake.call_args.args[0]
    assert cmd[0] == "fulcra"  # bare PATH lookup


def test_shell_out_failure_raises_runtimeerror(
    monkeypatch: pytest.MonkeyPatch, mocker
):
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    mocker.patch(
        "fulcra_attention.fulcra.subprocess.run",
        side_effect=subprocess.CalledProcessError(
            returncode=1, cmd=[], stderr=b"not logged in"
        ),
    )
    with pytest.raises(RuntimeError, match="fulcra auth print-access-token failed"):
        FulcraClient().get_token()

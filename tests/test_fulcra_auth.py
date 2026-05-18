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
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    fake = mocker.patch(
        "fulcra_attention.fulcra.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"shell-tok\n"
        ),
    )
    assert FulcraClient().get_token() == "shell-tok"
    fake.assert_called_once_with(
        ["fulcra", "auth", "print-access-token"],
        check=True,
        capture_output=True,
    )


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

import subprocess

import pytest

from fulcra_media.fulcra import FulcraClient


def test_get_token_calls_fulcra_auth(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"  fake.jwt.token  \n", stderr=b""
        ),
    )
    client = FulcraClient()
    assert client.get_token() == "fake.jwt.token"


def test_get_token_propagates_failure(mocker):
    err = subprocess.CalledProcessError(returncode=1, cmd=["fulcra"], stderr=b"not logged in")
    mocker.patch("subprocess.run", side_effect=err)
    client = FulcraClient()
    with pytest.raises(RuntimeError, match="fulcra auth print-access-token"):
        client.get_token()


def test_get_token_respects_env_override(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "env-token"})
    # subprocess should NOT be called when env var is set
    spy = mocker.patch("subprocess.run")
    client = FulcraClient()
    assert client.get_token() == "env-token"
    spy.assert_not_called()

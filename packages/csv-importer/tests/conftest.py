"""CSV tests cannot consult real credentials, Keychain, subprocesses or network."""

import base64
import json
import subprocess
import urllib.request

import httpx
import keyring
import pytest


@pytest.fixture(autouse=True)
def synthetic_boundaries(tmp_path, monkeypatch):
    from fulcra_api.credentials_store import CredentialsFile
    from fulcra_collect import macos_keychain

    claims = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "exp": 4102444800,
                    "fulcradynamics.com/userid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "synthetic." + claims + ".signature")
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "profile"))

    def forbidden(*args, **kwargs):
        pytest.fail("CSV test attempted external credential/network/process access")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    for method in ("get_password", "set_password", "delete_password", "get_keyring"):
        monkeypatch.setattr(keyring, method, forbidden)
    for method in ("get_password", "set_password", "delete_password"):
        if hasattr(macos_keychain, method):
            monkeypatch.setattr(macos_keychain, method, forbidden)
    monkeypatch.setattr(CredentialsFile, "load", forbidden)
    monkeypatch.setattr(CredentialsFile, "save", forbidden)

import base64
import json
import os
import socket
import subprocess

import httpx
import pytest


class NetflixTestIsolationBlocked(RuntimeError):
    """Static failure: never attach addresses, commands or credentials."""


@pytest.fixture(autouse=True)
def netflix_isolation(
    monkeypatch,
    _default_fulcra_access_token,
    _no_native_collect_keychain,
    _no_external_network,
):
    """Block escape paths before loading the canonical script, for one test.

    Explicit root-fixture dependencies ensure their broader defaults run first.
    This is a Python test boundary, not an OS sandbox; no children are allowed.
    Function scope prevents these blocks leaking into other package tests.
    """
    def blocked(*args, **kwargs):
        raise NetflixTestIsolationBlocked("Netflix test external action blocked")

    async def async_blocked(*args, **kwargs):
        blocked()

    for name in ("run", "Popen"):
        monkeypatch.setattr(subprocess, name, blocked)
    for name in ("system", "popen"):
        monkeypatch.setattr(os, name, blocked)
    for name in (
        "create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex",
        "gethostbyaddr", "getnameinfo",
    ):
        monkeypatch.setattr(socket, name, blocked)
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            monkeypatch.setattr(socket.socket, name, blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)

    # Import only after escape paths are blocked. These imports do not read a
    # credential; explicit tripwires also reject saved-auth and SDK fallback.
    import keyring
    from fulcra_api.core import FulcraAPI
    from fulcra_api.credentials_store import CredentialsFile
    from fulcra_api.oidc import FulcraOIDCProvider
    from fulcra_collect import credentials, macos_keychain
    from fulcra_collect.plugin import RunContext
    from fulcra_common.client import BaseFulcraClient

    monkeypatch.setattr(credentials, "_uses_macos_keychain", lambda: False)
    for name in (
        "_keyring_get", "get_secret", "set_secret", "delete_secret",
        "get_user_secret", "get_user_secret_fresh", "set_user_secret",
        "delete_user_secret", "refresh_fulcra_access_token",
    ):
        monkeypatch.setattr(credentials, name, blocked)
    for name in ("get_password", "set_password", "delete_password", "get_credential"):
        monkeypatch.setattr(keyring, name, blocked)
    monkeypatch.setattr(macos_keychain, "get_password", blocked)
    monkeypatch.setattr(RunContext, "collect_token", blocked)
    monkeypatch.setattr(BaseFulcraClient, "_lib", blocked)
    for name in ("load", "save", "lock"):
        monkeypatch.setattr(CredentialsFile, name, blocked)
    for name in (
        "get_token", "authorize", "refresh_access_token", "refresh_credentials",
        "_exchange_refresh_token",
    ):
        monkeypatch.setattr(FulcraAPI, name, blocked)
    for name in (
        "get_token", "refresh_credentials", "authorize_via_device_flow",
        "authorize_via_authorization_code_flow",
    ):
        monkeypatch.setattr(FulcraOIDCProvider, name, blocked)

    def segment(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    token = ".".join((segment({"alg": "none", "typ": "JWT"}), segment({
        "exp": 4102444800,
        "fulcradynamics.com/userid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    }), "synthetic-signature"))
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", token)
    monkeypatch.setattr(BaseFulcraClient, "get_token", lambda _: token)
    return {"error": NetflixTestIsolationBlocked, "token": token}


@pytest.fixture
def ni(netflix_isolation, monkeypatch):
    """Load only after guards; bind the legacy auth seam before any main call."""
    from fulcra_netflix import loader

    module = loader.load()
    if hasattr(module, "get_token"):
        monkeypatch.setattr(module, "get_token", lambda: netflix_isolation["token"])
    return module


@pytest.fixture()
def fixtures_dir(request):
    from pathlib import Path
    return Path(request.config.rootpath) / "packages/netflix-skill/tests/fixtures"

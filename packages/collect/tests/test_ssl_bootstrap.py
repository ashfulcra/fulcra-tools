"""CA-bundle bootstrap for the packaged macOS app."""
from __future__ import annotations

import ssl

from fulcra_collect import ssl_bootstrap


class _Paths:
    def __init__(self, cafile=None, capath=None):
        self.openssl_cafile = cafile
        self.openssl_capath = capath


def _no_platform_store(monkeypatch):
    monkeypatch.setattr(ssl, "get_default_verify_paths",
                        lambda: _Paths(cafile="/nonexistent/cert.pem"))


def test_noop_when_the_platform_ca_store_is_usable(monkeypatch, tmp_path):
    """Must not touch a working environment — this ships in the dev venv too."""
    real = tmp_path / "cert.pem"
    real.write_text("x")
    monkeypatch.setattr(ssl, "get_default_verify_paths",
                        lambda: _Paths(cafile=str(real)))
    env: dict = {}
    assert ssl_bootstrap.ensure_ca_bundle(env=env) is None
    assert env == {}


def test_sets_the_bundle_when_no_platform_store_exists(monkeypatch):
    _no_platform_store(monkeypatch)
    env: dict = {}
    result = ssl_bootstrap.ensure_ca_bundle(env=env)
    assert result is not None and result.endswith(".pem")
    # Both vars matter: the daemon shells out to the bundled fulcra CLI, and
    # the child inherits the environment, not an in-process SSLContext.
    assert env["SSL_CERT_FILE"] == result
    assert env["REQUESTS_CA_BUNDLE"] == result


def test_an_operator_supplied_bundle_is_never_overridden(monkeypatch):
    # Someone pointing us at a corporate CA bundle must win.
    _no_platform_store(monkeypatch)
    env = {"SSL_CERT_FILE": "/corp/ca.pem"}
    assert ssl_bootstrap.ensure_ca_bundle(env=env) is None
    assert env["SSL_CERT_FILE"] == "/corp/ca.pem"


def test_a_capath_directory_also_counts_as_usable(monkeypatch, tmp_path):
    monkeypatch.setattr(ssl, "get_default_verify_paths",
                        lambda: _Paths(cafile=None, capath=str(tmp_path)))
    env: dict = {}
    assert ssl_bootstrap.ensure_ca_bundle(env=env) is None
    assert env == {}


def test_missing_certifi_degrades_without_raising(monkeypatch):
    # A failure here must not take down the daemon at startup.
    _no_platform_store(monkeypatch)
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "certifi":
            raise ImportError("no certifi")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    env: dict = {}
    assert ssl_bootstrap.ensure_ca_bundle(env=env) is None
    assert env == {}

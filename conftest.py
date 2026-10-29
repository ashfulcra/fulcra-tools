"""Workspace-wide pytest fixtures (monorepo root).

The macOS CI suite is required to run hermetically — "Must NOT hit the network"
(see .github/workflows/macos.yml). Several packages' tests construct a Fulcra
client whose auth path shells out to ``fulcra auth print-access-token`` whenever
``FULCRA_ACCESS_TOKEN`` is unset. That subprocess escapes httpx's MockTransport
and needs real credentials, so the tests pass locally for an authed developer
but fail on a clean CI runner ("No credentials found"). The new macOS CI
surfaced this latent, cross-package debt.

This autouse fixture defaults a dummy access token for every test in the
workspace, so the auth path is satisfied without ever shelling out. It only sets
a test-env default — it changes no package logic. Tests that specifically
exercise the token-fetch / shell-out path (e.g. fulcra-common's
``test_get_token_*``) ``delenv`` or override it within their own body; because
``monkeypatch`` is function-scoped and applies after this autouse fixture, those
tests still see exactly the environment they intend.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

# pytest does not place the monorepo root on sys.path in every invocation.
_guard_spec = importlib.util.spec_from_file_location(
    "_pytest_network_guard", Path(__file__).with_name("_pytest_network_guard.py")
)
_guard = importlib.util.module_from_spec(_guard_spec)
sys.modules[_guard_spec.name] = _guard
_guard_spec.loader.exec_module(_guard)


@pytest.fixture(autouse=True)
def _default_fulcra_access_token(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-access-token-ci")


@pytest.fixture(autouse=True)
def _no_native_collect_keychain(monkeypatch):
    """Keep Collect credential tests off the real OS even on macOS.

    Tests historically monkeypatch keyring.get_password; the native quiet-read
    adapter intentionally bypasses that function. Only explicit injected-API
    tests opt back into its selection. Real native probes run separately with
    synthetic items, never the developer's saved credentials.
    """
    try:
        from fulcra_collect import credentials
    except ImportError:
        return  # This package's standalone environment need not install Collect.
    monkeypatch.setattr(credentials, '_uses_macos_keychain', lambda: False)


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch):
    """Keep ordinary Python test traffic local; see helper for scope limits."""
    from _pytest_network_guard import install

    install(monkeypatch)


@pytest.fixture(autouse=True)
def _private_trakt_owner(tmp_path, monkeypatch):
    """Never touch the OS-user Trakt journal; quiet writes default to refusal."""
    try:
        from fulcra_collect import credentials, trakt_source
    except ImportError:
        return
    original_root = trakt_source._os_user_root
    monkeypatch.setattr(trakt_source, '_os_user_root', lambda: tmp_path / 'trakt-owner')
    original = trakt_source.KeychainStore.__init__
    def synthetic_init(self, *, _quiet_write=None):
        def forbidden_write(*args):
            pytest.fail('Trakt quiet writes require an explicit isolated test backend')
        original(self, _quiet_write=_quiet_write or forbidden_write)
    monkeypatch.setattr(trakt_source.KeychainStore, '__init__', synthetic_init)
    original_authorize = credentials.authorize_secret
    def explicit_authorize(plugin, key):
        if plugin == 'trakt':
            pytest.fail('Trakt authorization requires an explicit isolated test backend')
        return original_authorize(plugin, key)
    monkeypatch.setattr(credentials, 'authorize_secret', explicit_authorize)
    return original_root

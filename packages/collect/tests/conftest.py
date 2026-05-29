"""Shared test fixtures for fulcra-collect."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_user_secret_cache():
    """Drop the in-process user-secret cache around every test.

    credentials.get_user_secret caches user secrets (the bearer token) in
    module state that outlives a single test. Without this, a token cached by
    one test would leak into the next (which expects a fresh, empty keychain),
    causing order-dependent failures."""
    from fulcra_collect import credentials as _creds
    _creds._clear_caches()
    yield
    _creds._clear_caches()


@pytest.fixture
def collect_home(tmp_path: Path, monkeypatch) -> Path:
    """Point the hub's config directory at a temp dir for the test.

    Closes any cached SQLite connections (Phase 1 of refactor #1) on
    teardown so a subsequent test's ``db.open()`` opens a fresh
    connection against its own tmp_path rather than reusing one that
    points at a now-deleted directory."""
    home = tmp_path / "collect-home"
    home.mkdir()
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(home))
    # Defensive: drop any connection cached from a previous test in the
    # same worker before the test starts touching state. (Pytest fixtures
    # are per-test by default, but the thread-local cache outlives them.)
    from fulcra_collect import db as _db
    _db.close_all()
    yield home
    _db.close_all()


@pytest.fixture
def _in_memory_keyring(monkeypatch):
    """Hermetic in-memory replacement for the system keyring.

    Replaces ``keyring.set_password`` / ``get_password`` / ``delete_password``
    on the ``fulcra_collect.credentials`` module with a dict-backed stub so
    tests that touch the credentials / auth code paths never reach the real
    OS keychain (which would prompt the user, mutate global state, or fail
    in CI). Used by every test that exercises sign-in, token rotation,
    OAuth callback handling, or any Daemon method that reads a stored
    bearer token (e.g. ``_delete_definition``).

    Returns the underlying store dict so tests can introspect what was
    written if they need to. ``PasswordDeleteError`` is raised on missing
    keys to mirror the real backend's contract."""
    store: dict[tuple[str, str], str] = {}

    def _set(service, key, value):
        store[(service, key)] = value

    def _get(service, key):
        return store.get((service, key))

    def _delete(service, key):
        import keyring.errors
        if (service, key) not in store:
            raise keyring.errors.PasswordDeleteError("not found")
        del store[(service, key)]

    import fulcra_collect.credentials as _creds_mod
    monkeypatch.setattr(_creds_mod.keyring, "set_password", _set)
    monkeypatch.setattr(_creds_mod.keyring, "get_password", _get)
    monkeypatch.setattr(_creds_mod.keyring, "delete_password", _delete)
    return store

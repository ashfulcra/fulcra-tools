"""Keychain-backed credential storage."""
from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend


class InMemoryKeyring(KeyringBackend):
    """A keyring backend that stores secrets in a dict — for tests only."""
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


@pytest.fixture(autouse=True)
def _in_memory_keyring(monkeypatch):
    backend = InMemoryKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    return backend


def test_set_then_get_round_trips():
    from fulcra_collect import credentials
    credentials.set_secret("lastfm", "api-key", "SECRET123")
    assert credentials.get_secret("lastfm", "api-key") == "SECRET123"


def test_get_missing_returns_none():
    from fulcra_collect import credentials
    assert credentials.get_secret("lastfm", "absent") is None


def test_delete_removes_the_secret():
    from fulcra_collect import credentials
    credentials.set_secret("lastfm", "api-key", "SECRET123")
    credentials.delete_secret("lastfm", "api-key")
    assert credentials.get_secret("lastfm", "api-key") is None


def test_secrets_are_namespaced_per_plugin():
    from fulcra_collect import credentials
    credentials.set_secret("lastfm", "token", "A")
    credentials.set_secret("trakt", "token", "B")
    assert credentials.get_secret("lastfm", "token") == "A"
    assert credentials.get_secret("trakt", "token") == "B"

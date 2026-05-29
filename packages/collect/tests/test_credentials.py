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


def test_keyring_read_that_blocks_times_out(monkeypatch):
    """A keychain read that blocks (macOS ACL-confirmation prompt) must NOT
    hang the caller forever — _keyring_get raises TimeoutError after the
    timeout. The daemon's control loop is single-threaded, so a forever-
    blocked read would otherwise wedge every request and surface as 'daemon
    not reachable'.
    """
    import threading
    import time

    from fulcra_collect import credentials

    blocking = threading.Event()  # never set → get_password blocks

    def _blocking_get(service, account):
        blocking.wait(timeout=5.0)  # simulate the unanswered keychain prompt
        return "should-never-be-returned"

    monkeypatch.setattr(keyring, "get_password", _blocking_get)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        credentials._keyring_get("svc", "acct", timeout=0.3)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0  # gave up promptly, didn't wait out the 5s block


def test_get_user_secret_returns_none_on_blocked_read_without_caching(monkeypatch):
    """A blocked keychain read degrades get_user_secret to None and must NOT
    cache that transient failure — a later read (once the prompt clears) must
    retry rather than return a stale 'absent'."""
    import threading

    from fulcra_collect import credentials

    blocking = threading.Event()

    def _blocking_get(service, account):
        blocking.wait(timeout=5.0)
        return "real-token"

    monkeypatch.setattr(keyring, "get_password", _blocking_get)
    monkeypatch.setattr(credentials, "_KEYCHAIN_READ_TIMEOUT_S", 0.3)
    assert credentials.get_user_secret("bearer-token") is None
    # Transient failure wasn't cached: the prompt "clears", reads succeed.
    blocking.set()
    monkeypatch.setattr(keyring, "get_password", lambda s, a: "real-token")
    assert credentials.get_user_secret("bearer-token") == "real-token"


def test_get_user_secret_caches_after_first_read(monkeypatch):
    """The bearer token is read on nearly every daemon operation; caching it
    means ONE keychain read per process (one macOS prompt) instead of ten.
    """
    from fulcra_collect import credentials

    calls = {"n": 0}

    def _counting_get(service, account):
        calls["n"] += 1
        return "tok"

    monkeypatch.setattr(keyring, "get_password", _counting_get)
    assert credentials.get_user_secret("bearer-token") == "tok"
    assert credentials.get_user_secret("bearer-token") == "tok"
    assert credentials.get_user_secret("bearer-token") == "tok"
    assert calls["n"] == 1  # only the first call hit the keychain


def test_keyring_read_propagates_real_errors(monkeypatch):
    """A genuine backend error (not a timeout) is re-raised on the caller
    thread rather than silently swallowed as a missing item."""
    from fulcra_collect import credentials

    def _boom(service, account):
        raise RuntimeError("keychain exploded")

    monkeypatch.setattr(keyring, "get_password", _boom)
    with pytest.raises(RuntimeError, match="keychain exploded"):
        credentials._keyring_get("svc", "acct", timeout=1.0)


def test_secrets_are_namespaced_per_plugin():
    from fulcra_collect import credentials
    credentials.set_secret("lastfm", "token", "A")
    credentials.set_secret("trakt", "token", "B")
    assert credentials.get_secret("lastfm", "token") == "A"
    assert credentials.get_secret("trakt", "token") == "B"


def test_has_secret_returns_true_when_secret_set(_in_memory_keyring):
    from fulcra_collect import credentials

    credentials.set_secret("lastfm", "session_key", "abc")
    assert credentials.has_secret("lastfm", "session_key") is True


def test_has_secret_returns_false_when_missing(_in_memory_keyring):
    from fulcra_collect import credentials

    assert credentials.has_secret("lastfm", "session_key") is False


def test_has_secret_returns_false_for_empty_string(_in_memory_keyring):
    # An empty string in the keychain counts as "no credential set" — the
    # menubar UI should still prompt the user to connect.
    from fulcra_collect import credentials

    credentials.set_secret("lastfm", "session_key", "")
    assert credentials.has_secret("lastfm", "session_key") is False


def test_delete_secret_is_idempotent_when_already_absent(monkeypatch):
    # The menubar Disconnect button calls delete_credential even when no
    # credential was ever stored (or was already cleared).  delete_secret must
    # swallow PasswordDeleteError so the user never sees a confusing error.
    import keyring
    import keyring.errors
    from fulcra_collect import credentials

    def _raising_delete(service, username):
        raise keyring.errors.PasswordDeleteError("not found")

    monkeypatch.setattr(keyring, "delete_password", _raising_delete)

    # Should not raise even though the underlying keyring always raises.
    credentials.delete_secret("lastfm", "session_key")
    # And calling it twice still doesn't raise:
    credentials.delete_secret("lastfm", "session_key")


# ---------------------------------------------------------------------------
# User-level credential helpers
# ---------------------------------------------------------------------------

def test_set_user_secret_stores(_in_memory_keyring):
    from fulcra_collect import credentials
    credentials.set_user_secret("bearer-token", "abc-secret")
    assert credentials.get_user_secret("bearer-token") == "abc-secret"


def test_has_user_secret_true_when_set(_in_memory_keyring):
    from fulcra_collect import credentials
    credentials.set_user_secret("bearer-token", "abc")
    assert credentials.has_user_secret("bearer-token") is True


def test_has_user_secret_false_when_missing(_in_memory_keyring):
    from fulcra_collect import credentials
    assert credentials.has_user_secret("bearer-token") is False


def test_has_user_secret_false_for_empty_string(_in_memory_keyring):
    from fulcra_collect import credentials
    credentials.set_user_secret("bearer-token", "")
    assert credentials.has_user_secret("bearer-token") is False


def test_delete_user_secret_is_idempotent(_in_memory_keyring):
    from fulcra_collect import credentials
    credentials.delete_user_secret("bearer-token")  # absent — should not raise
    credentials.set_user_secret("bearer-token", "x")
    credentials.delete_user_secret("bearer-token")
    credentials.delete_user_secret("bearer-token")  # idempotent — should not raise
    assert credentials.has_user_secret("bearer-token") is False


def test_user_secret_is_separate_from_plugin_secret(_in_memory_keyring):
    """User-level and plugin-level use different keyring service names
    so they don't collide."""
    from fulcra_collect import credentials
    credentials.set_secret("lastfm", "bearer-token", "plugin-value")
    credentials.set_user_secret("bearer-token", "user-value")
    assert credentials.get_secret("lastfm", "bearer-token") == "plugin-value"
    assert credentials.get_user_secret("bearer-token") == "user-value"

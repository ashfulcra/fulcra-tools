"""Lock + existence checks must treat the real store's MissingFileError as absent.

The real FulcraVaultStore.read_text/delete_explicit raise MissingFileError
(StoreError -> RuntimeError), NOT FileNotFoundError. Code that caught only
FileNotFoundError let the "file absent" path raise against the real store —
acquire() on a free lock, _exists() on a fresh vault, etc. The bundled fakes
hid this by raising FileNotFoundError.
"""
from datetime import datetime, timezone

from fulcra_vault.store import MissingFileError
from fulcra_vault import locks
from fulcra_vault.vault import _exists

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class MissingStore:
    def __init__(self):
        self.data = {}

    def read_text(self, path):
        if path not in self.data:
            raise MissingFileError(path)
        return self.data[path]

    def write_text(self, path, content):
        self.data[path] = content

    def delete_explicit(self, path, expected_stat=None):
        return bool(self.data.pop(path, None))


def test_acquire_treats_missing_lock_as_unlocked():
    tok = locks.acquire(MissingStore(), "Note", holder="a", now=NOW)
    assert tok.holder == "a"


def test_locked_contextmanager_acquire_release_with_missing_store():
    # exercises both _read_lock (acquire) and _delete_lock (release) catch sites
    store = MissingStore()
    with locks.locked(store, "Note", holder="a", now=NOW):
        pass  # must not raise MissingFileError on the free-lock / release paths


def test_exists_false_on_missing_file():
    assert _exists(MissingStore(), "/vault/x.md") is False

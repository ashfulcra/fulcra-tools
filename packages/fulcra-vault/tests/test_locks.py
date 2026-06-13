from datetime import datetime, timedelta, timezone

import pytest

from fulcra_vault.locks import LockError, LockHeldError, acquire, locked, release


NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.deleted: list[str] = []

    def read_text(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write_text(self, path: str, content: str) -> None:
        self.files[path] = content

    def delete_explicit(self, path: str, expected_stat=None) -> bool:
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]
        self.deleted.append(path)
        return True


def test_acquire_writes_lock_record_for_note():
    store = FakeStore()

    token = acquire(store, "Projects/Alpha", holder="agent-a", now=NOW)

    assert token.path == "/vault/.locks/Projects/Alpha.md.lock"
    assert '"holder":"agent-a"' in store.files[token.path]
    assert '"note":"Projects/Alpha.md"' in store.files[token.path]


def test_active_foreign_lock_fails_with_retry_hint():
    store = FakeStore()
    acquire(store, "Alpha", holder="agent-a", now=NOW)

    with pytest.raises(LockHeldError, match="held by agent-a"):
        acquire(store, "Alpha", holder="agent-b", now=NOW + timedelta(seconds=30))


def test_same_holder_refreshes_lock():
    store = FakeStore()
    acquire(store, "Alpha", holder="agent-a", now=NOW)

    refreshed = acquire(store, "Alpha", holder="agent-a", now=NOW + timedelta(seconds=10))

    assert refreshed.acquired_at == "2026-06-12T12:00:10+00:00"


def test_stale_lock_is_reaped_and_replaced():
    store = FakeStore()
    acquire(store, "Alpha", holder="agent-a", now=NOW)

    token = acquire(store, "Alpha", holder="agent-b", now=NOW + timedelta(seconds=121))

    assert token.holder == "agent-b"
    assert store.deleted == ["/vault/.locks/Alpha.md.lock"]


def test_release_requires_matching_holder():
    store = FakeStore()
    acquire(store, "Alpha", holder="agent-a", now=NOW)

    with pytest.raises(LockError, match="owned by agent-a"):
        release(store, "Alpha", holder="agent-b")

    assert release(store, "Alpha", holder="agent-a") is True
    assert "/vault/.locks/Alpha.md.lock" not in store.files


def test_context_manager_releases_on_success_and_exception():
    store = FakeStore()

    with locked(store, "Alpha", holder="agent-a", now=NOW):
        assert "/vault/.locks/Alpha.md.lock" in store.files

    assert "/vault/.locks/Alpha.md.lock" not in store.files

    with pytest.raises(RuntimeError):
        with locked(store, "Beta", holder="agent-a", now=NOW):
            raise RuntimeError("boom")

    assert "/vault/.locks/Beta.md.lock" not in store.files

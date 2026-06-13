"""Advisory per-note locks for agent writes."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Iterator, Protocol

from .schema import canonical_json, normalize_note_path


DEFAULT_TTL_SECONDS = 120


class LockError(RuntimeError):
    """Raised when a lock record is malformed or cannot be released."""


class LockHeldError(LockError):
    """Raised when another holder has an active lock."""


@dataclass(frozen=True)
class LockToken:
    path: str
    note: str
    holder: str
    acquired_at: str
    ttl_seconds: int


class LockStore(Protocol):
    def read_text(self, path: str) -> str:
        ...

    def write_text(self, path: str, content: str) -> None:
        ...

    def delete_explicit(self, path: str, expected_stat=None) -> bool:
        ...


def acquire(store: LockStore, note: str, *, holder: str, now: datetime,
            ttl_seconds: int = DEFAULT_TTL_SECONDS) -> LockToken:
    if not holder.strip():
        raise LockError("lock holder must not be empty")
    path = lock_path(note)
    normalized_note = normalize_note_path(note)
    existing = _read_lock(store, path)
    if existing is not None:
        active = not _is_stale(existing, now)
        owner = str(existing.get("holder", ""))
        if active and owner != holder:
            raise LockHeldError(
                f"{normalized_note} is held by {owner}; retry after lock release"
            )
        if not active:
            _delete_lock(store, path)
    token = LockToken(
        path=path,
        note=normalized_note,
        holder=holder,
        acquired_at=_stamp(now),
        ttl_seconds=ttl_seconds,
    )
    store.write_text(path, canonical_json({
        "acquired_at": token.acquired_at,
        "holder": token.holder,
        "note": token.note,
        "ttl_seconds": token.ttl_seconds,
    }) + "\n")
    return token


def release(store: LockStore, note: str, *, holder: str) -> bool:
    path = lock_path(note)
    existing = _read_lock(store, path)
    if existing is None:
        return False
    owner = str(existing.get("holder", ""))
    if owner != holder:
        raise LockError(f"lock for {normalize_note_path(note)} is owned by {owner}")
    return _delete_lock(store, path)


@contextmanager
def locked(store: LockStore, note: str, *, holder: str, now: datetime,
           ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Iterator[LockToken]:
    token = acquire(store, note, holder=holder, now=now, ttl_seconds=ttl_seconds)
    try:
        yield token
    finally:
        release(store, note, holder=holder)


def lock_path(note: str) -> str:
    return f"/vault/.locks/{normalize_note_path(note)}.lock"


def _read_lock(store: LockStore, path: str) -> dict | None:
    try:
        text = store.read_text(path)
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LockError(f"invalid lock record: {path}") from e
    if not isinstance(data, dict):
        raise LockError(f"invalid lock record: {path}")
    return data


def _is_stale(record: dict, now: datetime) -> bool:
    raw = record.get("acquired_at")
    ttl = record.get("ttl_seconds", DEFAULT_TTL_SECONDS)
    if not isinstance(raw, str) or not isinstance(ttl, int):
        raise LockError("invalid lock timestamp")
    try:
        acquired = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise LockError("invalid lock timestamp") from e
    if acquired.tzinfo is None:
        acquired = acquired.replace(tzinfo=timezone.utc)
    age = now.astimezone(timezone.utc) - acquired.astimezone(timezone.utc)
    return age.total_seconds() > ttl


def _delete_lock(store: LockStore, path: str) -> bool:
    try:
        return bool(store.delete_explicit(path))
    except FileNotFoundError:
        return False


def _stamp(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat()

"""The privacy ledger — append-only JSONL, one file per account.

The ledger is the durable record of what the relay did, holding **metadata and
hashes ONLY** — never email content. One file per account at
``<root>/gmail/<account_id>/ledger.jsonl`` (``root`` defaults to collect's
config home, ``~/.config/fulcra-collect``, and is injectable for tests).

**Append discipline.** Each :meth:`Ledger.append` writes exactly one JSONL
line, then ``flush()`` + ``os.fsync`` so the record is durable before the next
pipeline step runs. A crash mid-append can leave a torn final line;
:meth:`Ledger.entries` treats any unparseable line as ABSENT and skips it, so a
partial write is never fatal (worst case: one idempotent action repeats).

**Processed set.** Keyed by ``(message_id, rule_id, rule_version)``. A message
counts as processed for the contiguous-frontier cursor only when EVERY action
its rule requires has a ``done`` entry. Because the key includes
``rule_version``, bumping a rule's version starts a FRESH processed set —
old-version ``done`` entries don't match the new key.

**Relay outbox key.** A relay's :func:`outbox_key` is a deterministic function
of ``(account_id, message_id, rule_id, rule_version, "relay")``. The relay is
recorded ``pending`` (carrying the key) then ``done``; the byte-stable key is
what lets the bus leg (Task 3) dedupe retries to a single visible directive.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("fulcra_gmail.ledger")

ACTION_FILE = "file"
ACTION_RELAY = "relay"
STATUS_PENDING = "pending"
STATUS_DONE = "done"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_root() -> Path:
    """Collect's config home (respects ``FULCRA_COLLECT_HOME``)."""
    override = os.environ.get("FULCRA_COLLECT_HOME")
    if override:
        return Path(override)
    return Path.home() / ".config" / "fulcra-collect"


def outbox_key(
    account_id: str, message_id: str, rule_id: str, rule_version: int
) -> str:
    """Deterministic relay outbox key for ``(account, message, rule@version)``.

    Same inputs → identical string; a different ``rule_version`` (or any other
    component) → a different string. Built as a SHA-256 over NUL-joined
    components so operator-chosen ids can't collide via delimiter injection.
    """
    joined = "\x00".join(
        [account_id, message_id, rule_id, str(rule_version), ACTION_RELAY]
    )
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return f"relay-{digest}"


@dataclass(frozen=True)
class LedgerEntry:
    """One append-only ledger record. Metadata + hashes ONLY, never content."""

    ts: str
    account_id: str
    message_id: str
    rule_id: str
    rule_version: int
    action: str
    status: str
    sha256: str | None = None
    destination: str | None = None
    outbox_key: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    # -- convenience constructors (ts stamped now) --------------------------

    @classmethod
    def file_done(
        cls, *, account_id: str, message_id: str, rule_id: str,
        rule_version: int, sha256: str, destination: str,
    ) -> "LedgerEntry":
        return cls(
            ts=_iso_now(), account_id=account_id, message_id=message_id,
            rule_id=rule_id, rule_version=rule_version, action=ACTION_FILE,
            status=STATUS_DONE, sha256=sha256, destination=destination,
        )

    @classmethod
    def relay_pending(
        cls, *, account_id: str, message_id: str, rule_id: str,
        rule_version: int, outbox_key: str,
    ) -> "LedgerEntry":
        return cls(
            ts=_iso_now(), account_id=account_id, message_id=message_id,
            rule_id=rule_id, rule_version=rule_version, action=ACTION_RELAY,
            status=STATUS_PENDING, outbox_key=outbox_key,
        )

    @classmethod
    def relay_done(
        cls, *, account_id: str, message_id: str, rule_id: str,
        rule_version: int, outbox_key: str,
        sha256: str | None = None, destination: str | None = None,
    ) -> "LedgerEntry":
        return cls(
            ts=_iso_now(), account_id=account_id, message_id=message_id,
            rule_id=rule_id, rule_version=rule_version, action=ACTION_RELAY,
            status=STATUS_DONE, sha256=sha256, destination=destination,
            outbox_key=outbox_key,
        )


class Ledger:
    """Append-only JSONL ledger for one ``account_id``."""

    def __init__(self, account_id: str, *, root: Path | None = None) -> None:
        self.account_id = account_id
        base = root if root is not None else _default_root()
        self._path = base / "gmail" / account_id / "ledger.jsonl"
        self._entries_cache: list[dict] | None = None
        self._done_index: dict[tuple[str, str, int], set[str]] | None = None
        self._relay_keys: set[str] | None = None
        self._cache_signature: tuple[int, int, int] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def _lock_path(self) -> Path:
        return self._path.with_suffix(".lock")

    @contextmanager
    def _locked(self):
        """Serialize writers and compaction for this account's ledger."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a", encoding="utf-8") as lock_fh:
            os.chmod(self._lock_path, 0o600)
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    def _file_signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            return None
        return stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _append_unlocked(self, entry: LedgerEntry) -> None:
        """Append while the caller holds ``_locked``."""
        self.entries()  # refresh a cache made stale by another process
        line = json.dumps(entry.to_dict(), sort_keys=True) + "\n"
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(self._path, 0o600)
        assert self._entries_cache is not None
        row = entry.to_dict()
        self._entries_cache.append(row)
        self._index_row(row)
        self._cache_signature = self._file_signature()

    def append(self, entry: LedgerEntry) -> None:
        """Append one JSONL line, flushing + fsyncing before returning so the
        record is durable before the next pipeline step."""
        with self._locked():
            self._append_unlocked(entry)

    def _index_row(self, entry: dict) -> None:
        if self._done_index is None or self._relay_keys is None:
            return
        if entry.get("status") == STATUS_DONE:
            key = (
                str(entry.get("message_id", "")),
                str(entry.get("rule_id", "")),
                int(entry.get("rule_version", 0)),
            )
            self._done_index.setdefault(key, set()).add(str(entry.get("action", "")))
        outbox = entry.get("outbox_key")
        if entry.get("action") == ACTION_RELAY and isinstance(outbox, str) and outbox:
            self._relay_keys.add(outbox)

    def _rebuild_indexes(self) -> None:
        self._done_index = {}
        self._relay_keys = set()
        for entry in self._entries_cache or ():
            self._index_row(entry)

    def entries(self) -> list[dict]:
        """Read every intact JSONL record, skipping any torn/unparseable line.

        A partial final line (a crash mid-append) fails ``json.loads`` and is
        treated as ABSENT — never raised.
        """
        signature = self._file_signature()
        if self._entries_cache is not None and signature == self._cache_signature:
            return list(self._entries_cache)
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._entries_cache = []
            self._cache_signature = None
            self._rebuild_indexes()
            return []
        out: list[dict] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # Torn/partial line — treat as absent (do not crash).
                _log.debug("gmail ledger: skipping unparseable line (torn write)")
                continue
        self._entries_cache = out
        self._cache_signature = signature
        self._rebuild_indexes()
        return list(out)

    def ensure_relay_pending(self, entry: LedgerEntry) -> bool:
        """Persist a pending relay barrier once for its deterministic key.

        A retry still re-emits the byte-identical directive, but it does not
        append the same pending row forever. Returns ``True`` when a new row was
        written and ``False`` when this outbox key was already journaled.
        """
        if entry.action != ACTION_RELAY or entry.status != STATUS_PENDING:
            raise ValueError("ensure_relay_pending requires a pending relay entry")
        with self._locked():
            self.entries()
            assert self._relay_keys is not None
            if entry.outbox_key in self._relay_keys:
                return False
            self._append_unlocked(entry)
            return True

    @staticmethod
    def _canonical_entries(entries: list[dict]) -> list[dict]:
        """Collapse retry residue to one durable fact per task action."""
        chosen: dict[tuple[str, str, int, str], dict] = {}
        for entry in entries:
            try:
                key = (
                    str(entry["message_id"]), str(entry["rule_id"]),
                    int(entry["rule_version"]), str(entry["action"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            prior = chosen.get(key)
            if prior is None or entry.get("status") == STATUS_DONE:
                chosen[key] = entry
        action_order = {ACTION_FILE: 0, ACTION_RELAY: 1}
        return [
            chosen[key]
            for key in sorted(
                chosen,
                key=lambda value: (
                    value[0], value[1], value[2], action_order.get(value[3], 99),
                ),
            )
        ]

    def compact_if_needed(
        self, *, min_entries: int = 10_000, redundancy_ratio: float = 2.0,
    ) -> bool:
        """Atomically remove duplicate retry rows from an oversized ledger."""
        with self._locked():
            entries = self.entries()
            if len(entries) < min_entries:
                return False
            canonical = self._canonical_entries(entries)
            if not canonical or len(entries) < len(canonical) * redundancy_ratio:
                return False
            fd, raw_path = tempfile.mkstemp(
                prefix="ledger-", suffix=".jsonl.tmp", dir=self._path.parent,
            )
            tmp = Path(raw_path)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    for entry in canonical:
                        fh.write(json.dumps(entry, sort_keys=True) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, self._path)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
            self._entries_cache = canonical
            self._cache_signature = self._file_signature()
            self._rebuild_indexes()
            return True

    # -- processed set ------------------------------------------------------

    def done_actions(
        self, message_id: str, rule_id: str, rule_version: int
    ) -> set[str]:
        """The set of actions marked ``done`` for this exact
        ``(message_id, rule_id, rule_version)`` key."""
        self.entries()
        assert self._done_index is not None
        return set(self._done_index.get((message_id, rule_id, rule_version), set()))

    def is_fully_done(
        self, message_id: str, rule_id: str, rule_version: int,
        required_actions: list[str],
    ) -> bool:
        """True iff EVERY required action has a ``done`` entry for this key."""
        return set(required_actions) <= self.done_actions(
            message_id, rule_id, rule_version
        )

    def remaining_actions(
        self, message_id: str, rule_id: str, rule_version: int,
        required_actions: list[str],
    ) -> list[str]:
        """Required actions still lacking a ``done`` entry (input order kept)."""
        done = self.done_actions(message_id, rule_id, rule_version)
        return [a for a in required_actions if a not in done]

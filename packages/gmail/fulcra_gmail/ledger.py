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
import json
import logging
import os
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

    @property
    def path(self) -> Path:
        return self._path

    def append(self, entry: LedgerEntry) -> None:
        """Append one JSONL line, flushing + fsyncing before returning so the
        record is durable before the next pipeline step."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_dict(), sort_keys=True) + "\n"
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def entries(self) -> list[dict]:
        """Read every intact JSONL record, skipping any torn/unparseable line.

        A partial final line (a crash mid-append) fails ``json.loads`` and is
        treated as ABSENT — never raised.
        """
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
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
        return out

    # -- processed set ------------------------------------------------------

    def done_actions(
        self, message_id: str, rule_id: str, rule_version: int
    ) -> set[str]:
        """The set of actions marked ``done`` for this exact
        ``(message_id, rule_id, rule_version)`` key."""
        done: set[str] = set()
        for entry in self.entries():
            if (entry.get("message_id") == message_id
                    and entry.get("rule_id") == rule_id
                    and entry.get("rule_version") == rule_version
                    and entry.get("status") == STATUS_DONE):
                done.add(entry.get("action"))
        return done

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

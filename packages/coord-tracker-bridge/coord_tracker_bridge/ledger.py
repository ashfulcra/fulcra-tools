"""Crash-safe bridge state keyed by complete source identity."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import SourceIdentity


#: v2 adds `last_observed_at`. A v1 ledger loads with that field unset, which
#: reads as "never observed" — the fail-safe direction, because an entry whose
#: source row was never seen present can never authorize an absence-close.
LEDGER_SCHEMA_VERSION = 2
_SUPPORTED_SCHEMA_VERSIONS = frozenset({1, LEDGER_SCHEMA_VERSION})


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    source: SourceIdentity
    capability: str
    tracker_provider: str
    tracker_record_id: str
    policy_version: str
    policy_hash: str
    #: ISO timestamp of the last snapshot in which this entry's source row was
    #: actually PRESENT, or None if we have never seen it. Identity adopted from
    #: provider metadata (`_heal_ledger`) proves the tracker record exists; it
    #: proves nothing about the source row, so it leaves this None. `build_plan`
    #: refuses to close an entry for absence until this is set: otherwise
    #: "the enumeration was complete" gets read as "this row was deleted", which
    #: is the same over-claim this package keeps finding in its own surfaces.
    last_observed_at: str | None = None

    def __post_init__(self) -> None:
        values = (
            self.capability,
            self.tracker_provider,
            self.tracker_record_id,
            self.policy_version,
            self.policy_hash,
        )
        if not all(value.strip() for value in values):
            raise ValueError("ledger entry fields must be non-empty")

    @property
    def observed(self) -> bool:
        """Has this entry's source row ever been seen PRESENT in a snapshot?"""

        return bool(self.last_observed_at)

    def seen_at(self, observed_at: str) -> LedgerEntry:
        """Return a copy stamped with the snapshot that observed the row."""

        return replace(self, last_observed_at=observed_at)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source.to_dict(),
            "capability": self.capability,
            "tracker_provider": self.tracker_provider,
            "tracker_record_id": self.tracker_record_id,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
        }
        if self.last_observed_at is not None:
            payload["last_observed_at"] = self.last_observed_at
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LedgerEntry:
        observed = value.get("last_observed_at")
        return cls(
            source=SourceIdentity.from_dict(value["source"]),
            capability=str(value["capability"]),
            tracker_provider=str(value["tracker_provider"]),
            tracker_record_id=str(value["tracker_record_id"]),
            policy_version=str(value["policy_version"]),
            policy_hash=str(value["policy_hash"]),
            last_observed_at=None if observed is None else str(observed),
        )


class BridgeLedger:
    """In-memory ledger with deterministic, atomic JSON persistence."""

    def __init__(self, entries: Iterable[LedgerEntry] = ()) -> None:
        self._entries: dict[str, LedgerEntry] = {}
        for entry in entries:
            self.upsert(entry)

    def __iter__(self):
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, source: SourceIdentity) -> LedgerEntry | None:
        return self._entries.get(source.key)

    def upsert(self, entry: LedgerEntry) -> None:
        self._entries[entry.source.key] = entry

    def remove(self, source: SourceIdentity) -> None:
        self._entries.pop(source.key, None)

    def mark_observed(self, source: SourceIdentity, observed_at: str) -> bool:
        """Record that this source row was PRESENT in a snapshot.

        Returns whether anything changed, so callers can skip a needless write.
        Unknown sources are ignored: observation is provenance on an existing
        entry, never a way to mint one.
        """

        entry = self._entries.get(source.key)
        if entry is None or entry.last_observed_at == observed_at:
            return False
        self._entries[source.key] = entry.seen_at(observed_at)
        return True

    def to_dict(self) -> dict[str, Any]:
        entries = sorted(self._entries.values(), key=lambda entry: entry.source.key)
        return {"schema_version": LEDGER_SCHEMA_VERSION, "entries": [e.to_dict() for e in entries]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BridgeLedger:
        if value.get("schema_version") not in _SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError("unsupported ledger schema_version")
        # A v1 file carries no observation provenance, so every entry loads as
        # never-observed and no absence-close can fire until a run actually sees
        # the row. Migration is that read, not a rewrite that would invent
        # evidence the old file never recorded.
        return cls(LedgerEntry.from_dict(item) for item in value.get("entries", []))

    @classmethod
    def load(cls, path: str | Path) -> BridgeLedger:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("ledger root must be an object")
        return cls.from_dict(raw)

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"
        fd, staged = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staged, destination)
        finally:
            try:
                os.unlink(staged)
            except FileNotFoundError:
                pass

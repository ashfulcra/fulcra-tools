"""Crash-safe bridge state keyed by complete source identity."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import SourceIdentity


LEDGER_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    source: SourceIdentity
    capability: str
    tracker_provider: str
    tracker_record_id: str
    policy_version: str
    policy_hash: str

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "capability": self.capability,
            "tracker_provider": self.tracker_provider,
            "tracker_record_id": self.tracker_record_id,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LedgerEntry:
        return cls(
            source=SourceIdentity.from_dict(value["source"]),
            capability=str(value["capability"]),
            tracker_provider=str(value["tracker_provider"]),
            tracker_record_id=str(value["tracker_record_id"]),
            policy_version=str(value["policy_version"]),
            policy_hash=str(value["policy_hash"]),
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

    def to_dict(self) -> dict[str, Any]:
        entries = sorted(self._entries.values(), key=lambda entry: entry.source.key)
        return {"schema_version": LEDGER_SCHEMA_VERSION, "entries": [e.to_dict() for e in entries]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BridgeLedger:
        if value.get("schema_version") != LEDGER_SCHEMA_VERSION:
            raise ValueError("unsupported ledger schema_version")
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

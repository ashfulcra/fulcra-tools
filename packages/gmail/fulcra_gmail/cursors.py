"""Per-``(account, rule)`` contiguous-frontier cursors.

The B1 sync advances a watermark ONLY through the contiguous prefix of
fully-done effective matches (see :mod:`fulcra_gmail.pipeline`). That watermark
is one epoch-seconds integer per ``(account_id, rule_id, rule_version)``, stored
in a small JSON doc per account at ``<root>/gmail/<account_id>/cursors.json``.

Keying on ``rule_version`` (not just ``rule_id``) means a rule version bump
starts from a fresh (absent) cursor — consistent with the ledger's processed set,
which also keys on version. A ``None`` cursor drives the first-run window
(``newer_than:7d`` / backfill) in :func:`fulcra_gmail.rules.build_query`.

Writes are ``flush()`` + ``os.fsync`` + atomic ``replace`` so a crash can't leave
a torn cursor doc (worst case: the prior cursor survives and the overlap re-scans
— never a skipped message).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .ledger import _default_root


def _cursor_field(rule_id: str, rule_version: int) -> str:
    return f"{rule_id}@{rule_version}"


class CursorStore:
    """The per-account cursor doc. ``root`` is injectable for tests."""

    def __init__(self, account_id: str, *, root: Path | None = None) -> None:
        self.account_id = account_id
        base = root if root is not None else _default_root()
        self._path = base / "gmail" / account_id / "cursors.json"

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            # Unreadable/torn — treat as empty (over-capture beats a skip).
            return {}

    def get(self, rule_id: str, rule_version: int) -> int | None:
        value = self._read().get(_cursor_field(rule_id, rule_version))
        return int(value) if isinstance(value, (int, float)) else None

    def set(self, rule_id: str, rule_version: int, epoch_seconds: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        doc = self._read()
        doc[_cursor_field(rule_id, rule_version)] = int(epoch_seconds)
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self._path)

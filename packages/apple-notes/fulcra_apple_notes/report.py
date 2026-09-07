"""Durable per-run report for the Apple Notes sync.

The daemon runs plugins in a worker whose logger output does not reliably
reach a file an operator can read, which means a sync could silently do
nothing and still report ``done``. Every run therefore writes a structured
report to a known path: counts, timings, and the first errors. Absence of a
fresh report is itself the signal that a run did not happen.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os

REPORT_PATH = Path(os.path.expanduser(
    "~/Library/Logs/fulcra-collect/apple-notes-last-run.json"))
HISTORY_PATH = Path(os.path.expanduser(
    "~/Library/Logs/fulcra-collect/apple-notes-runs.jsonl"))
RECONCILE_PATH = Path(os.path.expanduser(
    "~/Library/Logs/fulcra-collect/apple-notes-reconcile.json"))
WRITEBACK_PATH = Path(os.path.expanduser(
    "~/Library/Logs/fulcra-collect/apple-notes-writeback.json"))


def write(payload: dict, *, path: Path | None = None,
          history: Path | None = None) -> None:
    target = path or REPORT_PATH
    hist = history or HISTORY_PATH
    payload = dict(payload)
    payload.setdefault("written_at", datetime.now(timezone.utc).isoformat())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, default=str))
        with hist.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        # Reporting must never be the thing that fails a sync.
        pass

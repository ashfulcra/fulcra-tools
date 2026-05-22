"""Per-plugin persisted state — last run, last outcome, failure count,
and the plugin's own watermark string. One JSON file per plugin under
the hub state directory. This is the snapshot the CLI and the UI read.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import config_dir


def _state_dir() -> Path:
    d = config_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class PluginState:
    plugin_id: str
    last_run: datetime | None = None
    last_outcome: str | None = None      # "done" | "error" | "timeout"
    last_error: str | None = None
    consecutive_failures: int = 0
    watermark: str | None = None         # ISO string, plugin-defined

    def record_finish(self, *, outcome: str, when: datetime,
                       error: str | None = None) -> None:
        """Record a finished run. A non-"done" outcome increments the
        consecutive-failure count; "done" resets it."""
        self.last_run = when
        self.last_outcome = outcome
        self.last_error = error
        if outcome == "done":
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1


def load(plugin_id: str) -> PluginState:
    path = _state_dir() / f"{plugin_id}.json"
    if not path.exists():
        return PluginState(plugin_id=plugin_id)
    doc = json.loads(path.read_text(encoding="utf-8"))
    lr = doc.get("last_run")
    return PluginState(
        plugin_id=plugin_id,
        last_run=datetime.fromisoformat(lr) if lr else None,
        last_outcome=doc.get("last_outcome"),
        last_error=doc.get("last_error"),
        consecutive_failures=doc.get("consecutive_failures", 0),
        watermark=doc.get("watermark"),
    )


def save(st: PluginState) -> None:
    doc = asdict(st)
    doc["last_run"] = st.last_run.isoformat() if st.last_run else None
    path = _state_dir() / f"{st.plugin_id}.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")

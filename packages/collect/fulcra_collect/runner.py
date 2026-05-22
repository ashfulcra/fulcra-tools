"""Execute one plugin run in a worker subprocess and record the outcome.

The runner spawns the worker, reads its JSON-line event stream, enforces
a per-run timeout, and writes the result to the plugin's PluginState.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime

from . import state

DEFAULT_TIMEOUT_S = 15 * 60


def worker_command(plugin_id: str) -> list[str]:
    """The command that runs the worker for `plugin_id`. Uses the current
    interpreter via `-m` so it works under a launchd/systemd minimal PATH."""
    import sys
    return [sys.executable, "-m", "fulcra_collect", "_worker", plugin_id]


def run(plugin_id: str, command: list[str], *, now: datetime,
        timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    """Run one plugin via `command`, record the outcome, return it
    ("done" | "error" | "timeout")."""
    outcome = "error"
    error: str | None = "worker emitted no result"
    watermark: str | None = None
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s,
        )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                outcome = event.get("outcome", "error")
                error = event.get("error")
                watermark = event.get("watermark")
    except subprocess.TimeoutExpired:
        outcome = "timeout"
        error = f"worker exceeded {timeout_s:.0f}s"

    st = state.load(plugin_id)
    # Persist the watermark the plugin advanced in the worker process. The
    # runner is the single writer of plugin state in the core process, so
    # the watermark crosses the worker boundary via the result event.
    if watermark is not None:
        st.watermark = watermark
    st.record_finish(outcome=outcome, when=now, error=error)
    state.save(st)
    return outcome

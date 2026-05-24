"""Execute one plugin run in a worker subprocess and record the outcome.

The runner spawns the worker, reads its JSON-line event stream, enforces
a per-run timeout, and writes the result to the plugin's PluginState.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from . import state

if TYPE_CHECKING:
    from .daemon import Daemon

DEFAULT_TIMEOUT_S = 15 * 60


def worker_command(plugin_id: str) -> list[str]:
    """The command that runs the worker for `plugin_id`. Uses the current
    interpreter via `-m` so it works under a launchd/systemd minimal PATH."""
    import sys
    return [sys.executable, "-m", "fulcra_collect", "_worker", plugin_id]


def run(plugin_id: str, command: list[str], *, now: datetime,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        on_spawn: Callable[[subprocess.Popen], None] | None = None,
        daemon: "Daemon | None" = None) -> str:
    """Run one plugin via `command`, record the outcome, return it
    ("done" | "error" | "timeout").

    If `on_spawn` is given it is called with the worker `Popen` right
    after the process is created, so a caller (the daemon) can track the
    process and terminate it on shutdown.

    If `daemon` is given, annotation events emitted by the worker are
    forwarded to ``daemon.activity`` so the web UI's dashboard "Recently"
    feed reflects real writes to Fulcra."""
    outcome = "error"
    error: str | None = "worker emitted no result"
    watermark: str | None = None
    definition_id: str | None = None
    try:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if on_spawn is not None:
            on_spawn(proc)
        try:
            stdout, _stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Mirror subprocess.run's internal timeout handling: kill the
            # worker, then drain its pipes so they are not left dangling.
            proc.kill()
            proc.communicate()
            raise
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "result":
                outcome = event.get("outcome", "error")
                error = event.get("error")
                watermark = event.get("watermark")
                definition_id = event.get("definition_id")
            elif event_type == "annotation":
                # Surface this in the daemon's activity buffer so the web UI's
                # dashboard "Recently" feed shows the receipt.
                if daemon is not None:
                    summary = event.get("summary", "")
                    ok = event.get("ok", True)
                    daemon.activity.add(
                        plugin_id=plugin_id, summary=summary, ok=ok,
                    )
    except subprocess.TimeoutExpired:
        outcome = "timeout"
        error = f"worker exceeded {timeout_s:.0f}s"

    st = state.load(plugin_id)
    # Persist values the plugin advanced in the worker process. The runner
    # is the single writer of plugin state in the core process, so both
    # watermark and definition_id cross the worker boundary via the result
    # event.
    if watermark is not None:
        st.watermark = watermark
    if definition_id is not None:
        st.definition_id = definition_id
    st.record_finish(outcome=outcome, when=now, error=error)
    state.save(st)
    return outcome

"""Execute one plugin run in a worker subprocess and record the outcome.

The runner spawns the worker, reads its JSON-line event stream, enforces
a per-run timeout, and writes the result to the plugin's PluginState.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from . import freshness, state

if TYPE_CHECKING:
    from .daemon import Daemon

DEFAULT_TIMEOUT_S = 15 * 60
MAX_STDOUT_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_STDERR_CAPTURE_BYTES = 512 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class _TailCapture:
    """Keep only the most recent bytes while continuously draining a pipe."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.total = 0

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if len(chunk) >= self.limit:
            self.data[:] = chunk[-self.limit:]
            return
        self.data.extend(chunk)
        excess = len(self.data) - self.limit
        if excess > 0:
            del self.data[:excess]

    @property
    def truncated(self) -> bool:
        return self.total > self.limit


@dataclass(frozen=True)
class _CapturedOutput:
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


def _bounded_communicate(
    proc: subprocess.Popen, *, timeout_s: float,
) -> _CapturedOutput:
    """Drain both worker pipes concurrently into bounded tail buffers."""
    stdout_capture = _TailCapture(MAX_STDOUT_CAPTURE_BYTES)
    stderr_capture = _TailCapture(MAX_STDERR_CAPTURE_BYTES)

    def drain(stream, capture: _TailCapture) -> None:
        try:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    return
                capture.append(chunk)
        finally:
            stream.close()

    if proc.stdout is None or proc.stderr is None:
        raise ValueError("worker process must expose stdout and stderr pipes")
    threads = (
        threading.Thread(target=drain, args=(proc.stdout, stdout_capture)),
        threading.Thread(target=drain, args=(proc.stderr, stderr_capture)),
    )
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:  # pragma: no cover - Collect's shipped app is macOS
            proc.kill()
        proc.wait()
    finally:
        for thread in threads:
            thread.join()

    captured = _CapturedOutput(
        stdout=bytes(stdout_capture.data),
        stderr=bytes(stderr_capture.data),
        stdout_truncated=stdout_capture.truncated,
        stderr_truncated=stderr_capture.truncated,
    )
    if timed_out:
        raise subprocess.TimeoutExpired(
            getattr(proc, "args", "worker"), timeout_s,
            output=captured.stdout,
            stderr=captured.stderr,
        )
    return captured


def worker_command(plugin_id: str) -> list[str]:
    """The command that runs the worker for `plugin_id`. Uses the current
    interpreter via `-m` so it works under a launchd/systemd minimal PATH."""
    import sys
    from pathlib import Path
    app_bin = Path(sys.executable).parent
    launcher = app_bin / "fulcra-collect"
    if app_bin.name == "MacOS" and app_bin.parent.name == "Contents" and launcher.is_file():
        return [str(launcher), "_worker", plugin_id]
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
    definition_validated_at: str | None = None
    # Track whether the worker pushed any annotation events at all so the
    # final run-summary entry (added at the bottom) can pick the right
    # message: "Ran, no new data" when the run was clean but quiet, vs.
    # nothing-extra when the user already saw "Recorded N items".
    annotation_count = 0
    # Counted separately from annotation_count, which includes ok=False
    # receipts (attempted-but-failed writes) because the activity feed shows
    # those too. A failed write is not a yield, so freshness must not treat it
    # as one — and changing annotation_count's meaning would alter the
    # "Ran successfully — no new data" feed entry it also drives.
    accepted_count = 0
    newest_observed_at: datetime | None = None
    spawned = False
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Plugins may start helper processes (for example, a CLI used to
            # write to an external service). Give each run its own process
            # group so a timeout stops the whole run rather than leaving a
            # grandchild working after Collect records "timeout".
            start_new_session=(os.name == "posix"),
        )
        spawned = True
        if on_spawn is not None:
            on_spawn(proc)
        captured = _bounded_communicate(proc, timeout_s=timeout_s)
        stdout = captured.stdout.decode("utf-8", errors="replace")
        stderr = captured.stderr.decode("utf-8", errors="replace")
        if captured.stderr_truncated:
            stderr = "[earlier stderr truncated]\n" + stderr
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
                definition_validated_at = event.get("definition_validated_at")
            elif event_type == "annotation":
                # Surface this in the daemon's activity buffer so the web UI's
                # dashboard "Recently" feed shows the receipt.
                annotation_count += 1
                if event.get("ok", True):
                    accepted_count += 1
                    # Instant comparison, not string comparison — see
                    # PluginState.record_yield for why the two disagree.
                    # record_yield re-validates, but picking the wrong
                    # candidate here would discard the right one.
                    observed = freshness.parse_instant(event.get("observed_at"))
                    if observed is not None and (
                        newest_observed_at is None or observed > newest_observed_at
                    ):
                        newest_observed_at = observed
                if daemon is not None:
                    summary = event.get("summary", "")
                    ok = event.get("ok", True)
                    daemon.activity.add(
                        plugin_id=plugin_id, summary=summary, ok=ok,
                    )
        if error == "worker emitted no result" and stderr.strip():
            # A crash before the worker's JSON result used to discard the only
            # useful diagnostic. Apply the same redaction and length bound as
            # normal worker exceptions before it reaches state or the UI.
            from .worker import _scrub_secrets
            error = _scrub_secrets(stderr.strip())
    except subprocess.TimeoutExpired:
        outcome = "timeout"
        error = f"worker exceeded {timeout_s:.0f}s"
    except OSError as exc:
        if spawned:
            raise
        # The daemon accepts a run before this background path reaches Popen.
        # A missing, quarantined, or otherwise unlaunchable worker therefore
        # has to become durable failed-run state here instead of escaping the
        # thread and leaving the dashboard stuck on the prior run forever.
        from .worker import _scrub_secrets
        error = _scrub_secrets(
            f"worker launch failed: {type(exc).__name__}: {exc}"
        )

    st = state.load(plugin_id)
    # Persist values the plugin advanced in the worker process. The runner
    # is the single writer of plugin state in the core process, so both
    # watermark and definition_id cross the worker boundary via the result
    # event.
    if watermark is not None:
        st.watermark = watermark
    if definition_id is not None:
        st.definition_id = definition_id
    if definition_validated_at is not None:
        st.definition_validated_at = definition_validated_at
    st.record_finish(outcome=outcome, when=now, error=error)
    # Freshness is recorded from what the run PRODUCED, never from its outcome.
    # A run that exits "done" having accepted nothing must leave last_yield_at
    # untouched — that untouched watermark is the entire signal.
    if accepted_count:
        st.record_yield(when=now, observed_at=newest_observed_at)
    state.save(st)

    # Add a run-summary entry to the activity feed so failures are visible
    # in the dashboard without users having to read state files on disk
    # (the gap that hid the Last.fm KeyError + the quick-record dead URL
    # bugs during the 2026-05-25 QA pass). The worker already scrubbed
    # secrets out of `error` before serialising it; we just truncate so
    # the feed stays readable.
    if daemon is not None:
        if outcome == "done":
            # Skip the "Ran, no new data" entry when the worker already
            # told the feed about specific writes — otherwise every
            # successful run with N writes ends up with N+1 entries.
            if annotation_count == 0:
                daemon.activity.add(
                    plugin_id=plugin_id,
                    summary="Ran successfully — no new data.",
                    ok=True,
                )
        else:
            # outcome is "error" or "timeout". Surface both.
            short_err = (error or "Plugin failed.").strip()
            # Keep summary on one screen line in the dashboard. The full
            # traceback already lives in state/<id>.json.last_error.
            if len(short_err) > 200:
                short_err = short_err[:200].rstrip() + "…"
            label = "timed out" if outcome == "timeout" else "failed"
            # First line of the (possibly multi-line) error makes the
            # most informative one-liner. Plugins emit "ExceptionName: msg"
            # as the first line of the traceback per worker._scrub_secrets.
            first_line = short_err.splitlines()[0] if short_err else ""
            daemon.activity.add(
                plugin_id=plugin_id,
                summary=f"Run {label}: {first_line}" if first_line
                        else f"Run {label}.",
                ok=False,
            )

    return outcome

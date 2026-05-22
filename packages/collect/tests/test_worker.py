"""The worker entrypoint — runs one plugin, emits JSON-line events."""
from __future__ import annotations

import io
import json
from pathlib import Path

from fulcra_collect import worker
from fulcra_collect.plugin import Plugin
from fulcra_collect.registry import RegistryResult
from fulcra_collect.worker import _scrub_secrets


def _run_capturing(plugin: Plugin, collect_home: Path) -> list[dict]:
    """Run a plugin through the worker, return the emitted JSON events."""
    buf = io.StringIO()
    worker.run_plugin(plugin, out=buf)
    return [json.loads(line) for line in buf.getvalue().splitlines() if line]


def test_worker_emits_a_done_result_for_a_successful_run(collect_home: Path):
    plugin = Plugin(id="ok", name="OK", kind="manual", run=lambda ctx: None)
    events = _run_capturing(plugin, collect_home)
    assert events[-1] == {"type": "result", "outcome": "done",
                          "error": None, "watermark": None}


def test_worker_carries_the_watermark_set_by_the_plugin(collect_home: Path):
    def run(ctx):
        ctx.state.watermark = "2026-05-22T12:00:00Z"
    plugin = Plugin(id="wm", name="WM", kind="manual", run=run)
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["watermark"] == "2026-05-22T12:00:00Z"


def test_worker_forwards_progress_events(collect_home: Path):
    def run(ctx):
        ctx.progress(done=1, total=3)
        ctx.progress(done=3, total=3)
    plugin = Plugin(id="p", name="P", kind="manual", run=run)
    events = _run_capturing(plugin, collect_home)
    progress = [e for e in events if e["type"] == "progress"]
    assert progress == [
        {"type": "progress", "done": 1, "total": 3},
        {"type": "progress", "done": 3, "total": 3},
    ]
    assert events[-1]["outcome"] == "done"


def test_worker_emits_an_error_result_when_run_raises(collect_home: Path):
    def run(ctx):
        raise RuntimeError("kaboom")
    plugin = Plugin(id="bad", name="Bad", kind="manual", run=run)
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["type"] == "result"
    assert events[-1]["outcome"] == "error"
    assert "kaboom" in events[-1]["error"]


def test_main_reports_unknown_plugin_id(collect_home: Path, capsys):
    rc = worker.main(["no-such-plugin"], registry=RegistryResult())
    captured = capsys.readouterr()
    last = [l for l in captured.out.splitlines() if l][-1]
    import json as _json
    assert _json.loads(last)["outcome"] == "error"
    assert rc == 1


def test_scrub_secrets_redacts_a_url_query_param():
    """M1: a secret-named URL query value is replaced, non-secret params kept."""
    text = "GET https://api.x/v1?api_key=ABC123&page=2 failed"
    scrubbed = _scrub_secrets(text)
    assert "ABC123" not in scrubbed
    assert "api_key=<redacted>" in scrubbed
    assert "page=2" in scrubbed  # non-secret param untouched


def test_scrub_secrets_redacts_a_bearer_token():
    """M1: a Bearer token in a traceback message is replaced."""
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.SECRETPART"
    scrubbed = _scrub_secrets(text)
    assert "SECRETPART" not in scrubbed
    assert "eyJhbGciOiJIUzI1NiJ9" not in scrubbed
    assert "<redacted>" in scrubbed


def test_scrub_secrets_leaves_non_secret_text_intact():
    """M1: ordinary error text passes through unchanged."""
    text = "RuntimeError: connection reset by peer at line 42"
    assert _scrub_secrets(text) == text


def test_scrub_secrets_truncates_a_pathological_traceback():
    """M1: the result is bounded so a huge traceback can't bloat state."""
    scrubbed = _scrub_secrets("x" * 10_000)
    assert len(scrubbed) <= 4000 + len("… (truncated)")
    assert scrubbed.endswith("… (truncated)")


def test_worker_error_result_scrubs_a_secret_in_the_exception(collect_home: Path):
    """M1: a secret raised in a plugin exception never reaches the event."""
    def run(ctx):
        raise RuntimeError("auth failed for https://api.x/v1?token=TOPSECRET")
    plugin = Plugin(id="leaky", name="Leaky", kind="manual", run=run)
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["outcome"] == "error"
    assert "TOPSECRET" not in events[-1]["error"]
    assert "token=<redacted>" in events[-1]["error"]


def test_worker_isolates_plugin_stdout_from_event_stream(
    collect_home: Path, monkeypatch, capsys,
):
    """Finding 9: a stray print() inside plugin.run must NOT corrupt the JSON
    event stream. The worker's runner parses `out` via splitlines() + json.loads
    and silently skips non-JSON lines, so a plain print() that lands between
    the progress and result emits would cause the result to be silently lost —
    the run is recorded as 'error' (no result emitted) and any watermark the
    plugin advanced gets dropped.

    Fix contract: stdout writes from *inside* plugin.run get redirected to
    stderr for the duration of the run, while the JSON event stream still
    goes to the `out` parameter the worker captured before the call. Mirrors
    the real worker entrypoint in `main()`, which passes the real `sys.stdout`
    as `out` — so `out` and `sys.stdout` are the same stream at call time.
    """
    import sys

    def run(ctx):
        # A library somewhere calls print(); the worker must not let this leak
        # into the JSON event stream that `out` carries.
        print("hello from a noisy library")
        ctx.state.watermark = "2026-05-22T12:00:00Z"

    plugin = Plugin(id="noisy", name="Noisy", kind="manual", run=run)
    # Re-bind sys.stdout to a buffer and pass it as `out` — same identity, as
    # in worker.main(). The whole point is that a stray print() (which writes
    # to whatever sys.stdout currently is) must not land on `out`.
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    worker.run_plugin(plugin, out=buf)
    # Restore stdout for capsys before assertions.
    monkeypatch.undo()

    lines = [l for l in buf.getvalue().splitlines() if l]
    # Every line on `out` must be valid JSON — no stray "hello..." string.
    parsed = [json.loads(l) for l in lines]
    assert parsed[-1]["type"] == "result"
    assert parsed[-1]["outcome"] == "done"
    assert parsed[-1]["watermark"] == "2026-05-22T12:00:00Z"


def test_worker_fails_fast_when_a_required_credential_is_missing(collect_home: Path):
    from fulcra_collect.plugin import Credential
    ran = []
    plugin = Plugin(id="needs-key", name="Needs Key", kind="manual",
                    run=lambda ctx: ran.append(True),
                    required_credentials=(Credential(key="api-key", label="K", help="h"),))
    events = _run_capturing(plugin, collect_home)
    assert ran == []  # run() was never called
    assert events[-1]["type"] == "result"
    assert events[-1]["outcome"] == "error"
    assert "api-key" in events[-1]["error"]

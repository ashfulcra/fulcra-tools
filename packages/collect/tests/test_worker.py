"""The worker entrypoint — runs one plugin, emits JSON-line events."""
from __future__ import annotations

import io
import json
from pathlib import Path

from fulcra_collect import worker
from fulcra_collect.plugin import Plugin
from fulcra_collect.registry import RegistryResult


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

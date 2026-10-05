"""The runner — spawns a worker subprocess for one run, records outcome."""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fulcra_collect import runner, state


def _python_worker(script: str) -> list[str]:
    """A command that runs `script` as the worker (emits its own JSON lines)."""
    return [sys.executable, "-c", script]


def test_runner_records_a_done_outcome(collect_home: Path):
    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done','error':None})+chr(10))"
    )
    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert outcome == "done"
    st = state.load("p")
    assert st.last_outcome == "done"
    assert st.consecutive_failures == 0


def test_runner_records_an_error_outcome(collect_home: Path):
    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'error','error':'boom'})+chr(10))"
    )
    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert outcome == "error"
    st = state.load("p")
    assert st.last_outcome == "error"
    assert st.last_error == "boom"
    assert st.consecutive_failures == 1


def test_runner_times_out_a_hung_worker(collect_home: Path):
    script = "import time; time.sleep(30)"
    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 22, tzinfo=timezone.utc),
                         timeout_s=1.0)
    assert outcome == "timeout"
    assert state.load("p").last_outcome == "timeout"


def test_bounded_capture_keeps_result_tail_without_unbounded_memory():
    result = b'\n{"type":"result","outcome":"done","error":null}\n'
    script = (
        "import sys;"
        f"sys.stdout.buffer.write(b'x'*{runner.MAX_STDOUT_CAPTURE_BYTES + 4096});"
        f"sys.stdout.buffer.write({result!r});"
        "sys.stdout.flush();"
        f"sys.stderr.buffer.write(b'e'*{runner.MAX_STDERR_CAPTURE_BYTES + 4096});"
        "sys.stderr.buffer.write(b'\\nuseful stderr tail\\n');"
        "sys.stderr.flush()"
    )
    proc = subprocess.Popen(
        _python_worker(script),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    captured = runner._bounded_communicate(proc, timeout_s=10)

    assert captured.stdout_truncated is True
    assert captured.stderr_truncated is True
    assert len(captured.stdout) <= runner.MAX_STDOUT_CAPTURE_BYTES
    assert len(captured.stderr) <= runner.MAX_STDERR_CAPTURE_BYTES
    assert captured.stdout.endswith(result)
    assert captured.stderr.endswith(b"useful stderr tail\n")


def test_runner_reads_a_final_result_after_noisy_output(collect_home: Path):
    script = (
        "import json,sys;"
        f"sys.stdout.write('noise'*{runner.MAX_STDOUT_CAPTURE_BYTES // 5 + 2048});"
        "sys.stdout.write(chr(10));"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done','error':None})+chr(10));"
        "sys.stdout.flush()"
    )

    outcome = runner.run(
        "p",
        _python_worker(script),
        now=datetime(2026, 5, 22, tzinfo=timezone.utc),
    )

    assert outcome == "done"


def test_runner_treats_a_worker_that_emits_no_result_as_error(collect_home: Path):
    script = "pass"  # exits cleanly but emits nothing
    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert outcome == "error"


def test_runner_records_worker_launch_failure(collect_home: Path, monkeypatch):
    """A missing/broken worker executable must become visible state.

    The daemon reports a run as started before its background thread calls
    Popen. If Popen then raises and the runner lets that exception escape, the
    user gets neither a failed run nor a dashboard receipt.
    """
    from fulcra_collect.activity import RecentActivity

    def fail_to_start(*args, **kwargs):
        raise FileNotFoundError("synthetic worker executable is missing")

    monkeypatch.setattr(runner.subprocess, "Popen", fail_to_start)
    activity = RecentActivity()

    class MockDaemon:
        pass

    daemon = MockDaemon()
    daemon.activity = activity

    outcome = runner.run(
        "p",
        ["missing-worker", "_worker", "p"],
        now=datetime(2026, 5, 22, tzinfo=timezone.utc),
        daemon=daemon,
    )

    assert outcome == "error"
    saved = state.load("p")
    assert saved.last_outcome == "error"
    assert "worker launch failed" in saved.last_error
    assert "FileNotFoundError" in saved.last_error
    entries = activity.recent()
    assert len(entries) == 1
    assert entries[0].ok is False
    assert "worker launch failed" in entries[0].summary


def test_runner_keeps_scrubbed_stderr_when_worker_crashes(collect_home: Path):
    script = (
        "import sys;"
        "sys.stderr.write('crashed with Authorization: Bearer super-secret-token\\n');"
        "sys.exit(2)"
    )
    outcome = runner.run(
        "p", _python_worker(script),
        now=datetime(2026, 5, 22, tzinfo=timezone.utc),
    )
    assert outcome == "error"
    error = state.load("p").last_error
    assert "crashed with Authorization" in error
    assert "super-secret-token" not in error
    assert "Bearer <redacted>" in error


def test_runner_persists_the_watermark_from_the_result(collect_home: Path):
    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done',"
        "'error':None,'watermark':'2026-05-22T12:00:00Z'})+chr(10))"
    )
    runner.run("p", _python_worker(script),
               now=datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert state.load("p").watermark == "2026-05-22T12:00:00Z"


def test_runner_persists_the_definition_id_from_the_result(collect_home: Path):
    """Important 1: definition_id set by the worker must cross the subprocess
    boundary and be written to saved state — mirroring how watermark already
    travels the same path."""
    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done',"
        "'error':None,'watermark':None,'definition_id':'def-abc123'})+chr(10))"
    )
    runner.run("p", _python_worker(script),
               now=datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert state.load("p").definition_id == "def-abc123"


def test_runner_forwards_annotation_events_to_activity_buffer(collect_home: Path):
    """When a worker emits an annotation event, the runner forwards it to
    daemon.activity so the web UI's dashboard "Recently" feed reflects real
    annotation writes."""
    from fulcra_collect.activity import RecentActivity

    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'annotation','summary':'Listened: 3 new scrobbles','ok':True})+chr(10));"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done','error':None})+chr(10))"
    )

    activity = RecentActivity()

    class MockDaemon:
        pass

    daemon = MockDaemon()
    daemon.activity = activity

    outcome = runner.run("lastfm", _python_worker(script),
                         now=datetime(2026, 5, 24, tzinfo=timezone.utc),
                         daemon=daemon)
    assert outcome == "done"
    entries = activity.recent()
    assert len(entries) == 1
    assert entries[0].plugin_id == "lastfm"
    assert entries[0].summary == "Listened: 3 new scrobbles"
    assert entries[0].ok is True


def test_runner_ignores_annotation_events_without_daemon(collect_home: Path):
    """Annotation events are silently ignored when no daemon is supplied —
    no crash, no activity side-effects."""
    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'annotation','summary':'x','ok':True})+chr(10));"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done','error':None})+chr(10))"
    )
    # Passes no daemon= — must not raise
    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 24, tzinfo=timezone.utc))
    assert outcome == "done"


def test_runner_forwards_multiple_annotation_events(collect_home: Path):
    """Multiple annotation events from one run all land in the buffer."""
    from fulcra_collect.activity import RecentActivity

    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'annotation','summary':'A','ok':True})+chr(10));"
        "sys.stdout.write(json.dumps({'type':'annotation','summary':'B','ok':False})+chr(10));"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done','error':None})+chr(10))"
    )

    activity = RecentActivity()

    class MockDaemon:
        pass

    daemon = MockDaemon()
    daemon.activity = activity

    runner.run("p", _python_worker(script),
               now=datetime(2026, 5, 24, tzinfo=timezone.utc), daemon=daemon)
    entries = activity.recent()
    # recent() returns newest-first, so B is first
    assert len(entries) == 2
    summaries = {e.summary for e in entries}
    assert summaries == {"A", "B"}


def test_runner_records_a_run_summary_when_done_with_no_annotations(collect_home: Path):
    """After 2026-05-25, the runner appends a neutral 'Ran successfully — no
    new data.' entry to the activity feed when a clean run produces zero
    annotation events. Without this, the dashboard's RECENTLY section was
    silent on every quiet run and users couldn't tell whether the plugin
    was alive."""
    from fulcra_collect.activity import RecentActivity

    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done','error':None})+chr(10))"
    )
    activity = RecentActivity()

    class MockDaemon:
        pass
    daemon = MockDaemon()
    daemon.activity = activity

    outcome = runner.run("lastfm", _python_worker(script),
                         now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                         daemon=daemon)
    assert outcome == "done"
    entries = activity.recent()
    assert len(entries) == 1
    assert entries[0].plugin_id == "lastfm"
    assert entries[0].ok is True
    assert "no new data" in entries[0].summary.lower()


def test_runner_does_not_double_log_when_done_with_annotations(collect_home: Path):
    """When the worker already emitted per-annotation events, the runner
    must NOT also append a 'Ran successfully' summary — otherwise every
    run with N writes ends up as N+1 entries and the feed gets noisy."""
    from fulcra_collect.activity import RecentActivity

    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'annotation','summary':'Recorded 2 scrobbles','ok':True})+chr(10));"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done','error':None})+chr(10))"
    )
    activity = RecentActivity()

    class MockDaemon:
        pass
    daemon = MockDaemon()
    daemon.activity = activity

    outcome = runner.run("lastfm", _python_worker(script),
                         now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                         daemon=daemon)
    assert outcome == "done"
    entries = activity.recent()
    # Only the worker's per-annotation entry — no extra synthetic summary.
    assert len(entries) == 1
    assert entries[0].summary == "Recorded 2 scrobbles"


def test_runner_records_a_failure_summary_on_error_outcome(collect_home: Path):
    """An error outcome lands a single ok=False activity entry whose
    summary starts with 'Run failed:' and includes the worker's first
    error line. Regression for the 2026-05-25 gap that hid every plugin
    failure from the dashboard."""
    from fulcra_collect.activity import RecentActivity

    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'error',"
        "'error':\"KeyError: 'username'\\nfile.py line 99\"})+chr(10))"
    )
    activity = RecentActivity()

    class MockDaemon:
        pass
    daemon = MockDaemon()
    daemon.activity = activity

    outcome = runner.run("lastfm", _python_worker(script),
                         now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                         daemon=daemon)
    assert outcome == "error"
    entries = activity.recent()
    assert len(entries) == 1
    assert entries[0].plugin_id == "lastfm"
    assert entries[0].ok is False
    # First line of the worker's error message goes into the summary;
    # the multi-line traceback stays in state/<id>.json.last_error.
    assert entries[0].summary.startswith("Run failed:")
    assert "KeyError" in entries[0].summary
    assert "file.py line 99" not in entries[0].summary


def test_runner_records_a_timeout_summary_on_timeout_outcome(collect_home: Path):
    """Timeouts also surface as failures in the activity feed (with a
    distinct 'timed out' label rather than 'failed') so the user can tell
    a hung plugin from a crashing one."""
    from fulcra_collect.activity import RecentActivity

    # Sleeps for 30s — well past our 0.5s timeout.
    script = "import time; time.sleep(30)"
    activity = RecentActivity()

    class MockDaemon:
        pass
    daemon = MockDaemon()
    daemon.activity = activity

    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                         timeout_s=0.5, daemon=daemon)
    assert outcome == "timeout"
    entries = activity.recent()
    assert len(entries) == 1
    assert entries[0].plugin_id == "p"
    assert entries[0].ok is False
    assert "timed out" in entries[0].summary.lower()


def test_runner_calls_on_spawn_with_the_worker_process(collect_home: Path):
    """`on_spawn` is invoked with the live worker Popen so a caller (the
    daemon) can track it and terminate it on shutdown."""
    import subprocess

    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done','error':None})+chr(10))"
    )
    spawned: list[subprocess.Popen] = []
    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 22, tzinfo=timezone.utc),
                         on_spawn=spawned.append)
    assert outcome == "done"
    assert len(spawned) == 1
    assert isinstance(spawned[0], subprocess.Popen)
    # the worker has been awaited, so it is finished by the time run returns
    assert spawned[0].poll() is not None


def test_runner_persists_definition_validated_at_from_the_result(collect_home: Path):
    """The definition-validation gate's watermark must cross the worker
    boundary exactly like watermark/definition_id, or the gate re-validates
    every run and the TTL never engages."""
    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done',"
        "'error':None,'watermark':None,'definition_id':'def-abc123',"
        "'definition_validated_at':'2026-07-06T12:00:00+00:00'})+chr(10))"
    )
    runner.run("p", _python_worker(script),
               now=datetime(2026, 7, 6, tzinfo=timezone.utc))
    st = state.load("p")
    assert st.definition_validated_at == "2026-07-06T12:00:00+00:00"


def test_bundled_worker_uses_app_cli_not_python_flags(tmp_path, monkeypatch):
    appbin = tmp_path / 'Collect.app' / 'Contents' / 'MacOS'
    appbin.mkdir(parents=True)
    launcher = appbin / 'fulcra-collect'
    launcher.write_text('#!/bin/sh\n')
    monkeypatch.setattr(sys, 'executable', str(appbin / 'Fulcra Collect'))
    assert runner.worker_command('apple-notes') == [str(launcher), '_worker', 'apple-notes']

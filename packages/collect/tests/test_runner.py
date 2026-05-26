"""The runner — spawns a worker subprocess for one run, records outcome."""
from __future__ import annotations

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


def test_runner_treats_a_worker_that_emits_no_result_as_error(collect_home: Path):
    script = "pass"  # exits cleanly but emits nothing
    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert outcome == "error"


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

    class MockDaemon: pass
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

    class MockDaemon: pass
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

    class MockDaemon: pass
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

    class MockDaemon: pass
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

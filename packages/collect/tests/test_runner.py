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

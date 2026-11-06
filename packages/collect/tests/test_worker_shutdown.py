"""Real owned children; no credentials, provider calls or daemon startup."""
import os
import subprocess
import sys
import threading
import time

import pytest

from fulcra_collect import runner, supervisor
from fulcra_collect.config import Config
from fulcra_collect.daemon import Daemon
from fulcra_collect.registry import RegistryResult


def test_shutdown_closes_launches_accepted_before_scope_capture(collect_home, monkeypatch):
    daemon = Daemon(registry=RegistryResult(), config=Config())
    captured, release = threading.Event(), threading.Event()
    def capture(_):
        captured.set()
        assert release.wait(3)
    monkeypatch.setattr(daemon, '_capture_worker_scope', capture)
    monkeypatch.setattr(runner, 'worker_command', lambda _: [sys.executable, '-c', 'import time;time.sleep(60)'])
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: pytest.fail('late worker launched'))
    daemon._trigger('synthetic')
    assert captured.wait(2)
    stopped = threading.Thread(target=daemon._shutdown_workers)
    stopped.start()
    try:
        assert daemon._closing.wait(2)
    finally:
        release.set()
        stopped.join(4)
    assert not stopped.is_alive()
    assert not daemon._inflight
    assert daemon._trigger('synthetic') is False


@pytest.mark.skipif(os.name != 'posix', reason='owned process groups')
def test_cancelled_runner_reaps_term_ignoring_worker(tmp_path):
    ready = tmp_path / 'ready'
    child = subprocess.Popen([sys.executable, '-c',
        'import signal,time,pathlib,sys;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
        'pathlib.Path(sys.argv[1]).touch();time.sleep(60)', str(ready)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    deadline = time.monotonic() + 3
    try:
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        cancelled = threading.Event()
        cancelled.set()
        with pytest.raises(runner.WorkerCancelled):
            runner._bounded_communicate(child, timeout_s=60, cancelled=cancelled)
        assert child.returncode is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)


@pytest.mark.skipif(os.name != 'posix', reason='owned process groups')
def test_supervisor_shutdown_reaps_term_ignoring_service(tmp_path):
    ready = tmp_path / 'ready'
    child = subprocess.Popen([sys.executable, '-c',
        'import signal,time,pathlib,sys;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
        'pathlib.Path(sys.argv[1]).touch();time.sleep(60)', str(ready)], start_new_session=True)
    sup = supervisor.ServiceSupervisor()
    sup._procs['synthetic'] = child
    deadline = time.monotonic() + 3
    try:
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        sup.shutdown_all()
        assert child.returncode is not None
        assert not sup._procs
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)


def test_cleanup_never_signals_a_reaped_group(monkeypatch):
    class Reaped:
        returncode = 0
        pid = 12345
    monkeypatch.setattr(supervisor.os, 'killpg', lambda *a: pytest.fail('reaped identity signalled'))
    supervisor.stop_owned_processes([Reaped()])

"""Tests for `fulcra-media webhook` long-running CLI.

These tests exercise the click command end-to-end:
- Missing watched def → exit-2 envelope, no server bind
- Non-loopback host without bearer-token → exit-2 envelope
- Happy path: server starts, prints a JSON ready-line, shuts down on
  SIGTERM/SIGINT and prints a JSON shutdown-line

The happy path runs the CLI in a subprocess (so we get a real signal
handler context) and reads stdout/stderr to assert the lifecycle lines.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest
from click.testing import CliRunner

from fulcra_media.cli import cli
from fulcra_media.state import State


@pytest.fixture
def fake_state(tmp_path, monkeypatch):
    """Point the CLI at a tmp state.json with a Watched def set."""
    state_path = tmp_path / "state.json"
    s = State(
        watched_definition_id="def-watched-uuid",
        tag_ids={"plex": "tag-plex"},
    )
    from fulcra_media import state as state_mod
    state_mod.save(s, state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)
    return state_path


def test_webhook_missing_watched_def_emits_setup_error(tmp_path, monkeypatch):
    """No watched def → ok=false, errors stage=setup, exit 2."""
    state_path = tmp_path / "state.json"
    from fulcra_media import state as state_mod
    state_mod.save(State(), state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)

    res = CliRunner().invoke(cli, ["webhook", "--json"])
    assert res.exit_code == 2, res.output
    payload = json.loads(res.output.strip().splitlines()[0])
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "setup"


def test_webhook_non_loopback_without_token_emits_args_error(fake_state):
    """--host 0.0.0.0 without --bearer-token → exit 2, args error."""
    res = CliRunner().invoke(cli, ["webhook", "--host", "0.0.0.0", "--json"])
    assert res.exit_code == 2, res.output
    payload = json.loads(res.output.strip().splitlines()[0])
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "args"
    assert "non-loopback" in payload["errors"][0]["message"]


def test_webhook_loopback_default_does_not_require_token(fake_state, monkeypatch):
    """--host 127.0.0.1 (default) is allowed without a token; we don't
    actually block on serve here — we mock make_server to assert the
    code path reaches it without erroring out on validation."""
    called = {}

    def fake_make_server(**kwargs):
        called.update(kwargs)
        raise RuntimeError("STOP — we only need to confirm we got this far")

    monkeypatch.setattr(
        "fulcra_media.webhook_receiver.make_server", fake_make_server,
    )
    res = CliRunner().invoke(cli, ["webhook", "--json"])
    # The RuntimeError bubbles to the click runner.
    assert isinstance(res.exception, RuntimeError)
    assert called["host"] == "127.0.0.1"
    assert called["bearer_token"] is None


# ----------------------------------------------------------------------
# Subprocess-driven happy path (needs a real signal context)
# ----------------------------------------------------------------------

def _ready_line(proc, timeout: float = 10.0):
    """Wait for the ready-line on stdout, return its parsed JSON."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.05)
            continue
        try:
            return json.loads(line.decode("utf-8").strip())
        except json.JSONDecodeError:
            continue
    raise TimeoutError("never saw a JSON ready-line on stdout")


def test_webhook_starts_emits_ready_and_shuts_down_on_sigterm(
    tmp_path, monkeypatch,
):
    """Full lifecycle: start server in a subprocess, see the ready-line,
    SIGTERM, see the shutdown-line, exit 0."""
    # Pre-populate state.json with a watched def so the server can boot.
    state_path = tmp_path / "state.json"
    from fulcra_media import state as state_mod
    state_mod.save(
        State(watched_definition_id="def-watched-uuid"),
        state_path,
    )

    env = os.environ.copy()
    env["FULCRA_MEDIA_STATE"] = str(state_path)
    # Don't try to refresh a real Fulcra token in the subprocess.
    env["FULCRA_ACCESS_TOKEN"] = "fake-test-token"
    # Make stdout line-buffered through Python.
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [
        sys.executable, "-m", "fulcra_media.cli",
        "webhook", "--host", "127.0.0.1", "--port", "0",
        "--bearer-token", "test-token", "--json",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        ready = _ready_line(proc)
        assert ready["ok"] is True
        assert ready["stage"] == "ready"
        assert ready["host"] == "127.0.0.1"
        assert ready["port"] > 0
        assert ready["bearer_token_required"] is True

        # Send SIGTERM and expect a clean exit
        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        # The shutdown line should be on stdout (json mode).
        remaining = proc.stdout.read().decode("utf-8").splitlines()
        assert remaining, "no shutdown line"
        shutdown = json.loads(remaining[-1])
        assert shutdown["stage"] == "shutdown"
        assert shutdown["ok"] is True
        assert rc == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)


def test_webhook_starts_and_shuts_down_on_sigint(tmp_path):
    """Same as above but with SIGINT (Ctrl-C) — same clean exit path."""
    state_path = tmp_path / "state.json"
    from fulcra_media import state as state_mod
    state_mod.save(
        State(watched_definition_id="def-watched-uuid"),
        state_path,
    )

    env = os.environ.copy()
    env["FULCRA_MEDIA_STATE"] = str(state_path)
    env["FULCRA_ACCESS_TOKEN"] = "fake-test-token"
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [
        sys.executable, "-m", "fulcra_media.cli",
        "webhook", "--port", "0", "--bearer-token", "x", "--json",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        ready = _ready_line(proc)
        assert ready["stage"] == "ready"
        proc.send_signal(signal.SIGINT)
        try:
            rc = proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        assert rc == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)

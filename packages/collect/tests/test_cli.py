"""The fulcra-collect CLI."""
from __future__ import annotations

from pathlib import Path

import pytest

from click.testing import CliRunner

from fulcra_collect import config as config_mod
from fulcra_collect.cli import cli


@pytest.mark.parametrize("args,expected_launch", [
    (["daemon"], True),
    (["daemon", "--no-menubar"], False),
])
def test_daemon_routes_menubar_startup_option(monkeypatch, args, expected_launch):
    launches = []

    class StartupDaemon:
        def __init__(self, *, launch_menubar):
            self.launch_menubar = launch_menubar

        def serve(self):
            launches.append(self.launch_menubar)

    monkeypatch.setattr("fulcra_collect.cli.Daemon", StartupDaemon)
    monkeypatch.setattr("fulcra_collect.cli._stderr_is_a_tty", lambda: False)

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert launches == [expected_launch]


def test_daemon_foreground_prints_durability_hint_on_tty(monkeypatch):
    """A foreground `fulcra-collect daemon` should print a one-line hint
    pointing first-time operators at the launchd install path — but ONLY when
    stderr is a real TTY (under launchd there's no TTY and the hint would just
    clutter the log file)."""
    monkeypatch.setattr("fulcra_collect.cli._stderr_is_a_tty", lambda: True)

    # Stub Daemon().serve() so we don't actually start a server.
    class _StubDaemon:
        def __init__(self, *, launch_menubar):
            pass

        def serve(self):  # noqa: D401 — mirrors real Daemon.serve
            return None

    monkeypatch.setattr("fulcra_collect.cli.Daemon", _StubDaemon)

    res = CliRunner().invoke(cli, ["daemon"])
    assert res.exit_code == 0
    # Hint goes to stderr (err=True in click.echo). Check both for portability.
    combined = (res.stderr or "") + (res.output or "")
    assert "running in the foreground" in combined
    assert "fulcra-collect install" in combined
    assert "launchctl bootstrap" in combined


def test_daemon_under_launchd_stays_quiet(monkeypatch):
    """When stderr is NOT a TTY (launchd), the durability hint must not print —
    it would just spam ~/Library/Logs/fulcra-collect/daemon.err.log."""
    monkeypatch.setattr("fulcra_collect.cli._stderr_is_a_tty", lambda: False)

    class _StubDaemon:
        def __init__(self, *, launch_menubar):
            pass

        def serve(self):
            return None
    monkeypatch.setattr("fulcra_collect.cli.Daemon", _StubDaemon)

    res = CliRunner().invoke(cli, ["daemon"])
    assert res.exit_code == 0
    combined = (res.stderr or "") + (res.output or "")
    assert "running in the foreground" not in combined


def test_enable_then_disable_update_config(collect_home: Path):
    runner = CliRunner()
    assert runner.invoke(cli, ["enable", "lastfm"]).exit_code == 0
    assert config_mod.load().enabled == {"lastfm"}
    assert runner.invoke(cli, ["disable", "lastfm"]).exit_code == 0
    assert config_mod.load().enabled == set()


def test_set_interval_writes_an_override(collect_home: Path):
    res = CliRunner().invoke(cli, ["set-interval", "lastfm", "1800"])
    assert res.exit_code == 0
    assert config_mod.load().interval_overrides == {"lastfm": 1800}


def test_status_reports_when_the_daemon_is_not_running(collect_home: Path):
    res = CliRunner().invoke(cli, ["status"])
    # No daemon -> a clean message, non-zero exit, not a traceback.
    assert res.exit_code != 0
    assert "daemon" in res.output.lower()


def test_status_prints_a_snapshot_from_a_stub_daemon(collect_home: Path, monkeypatch):
    snapshot = {"ok": True, "plugins": [
        {"id": "lastfm", "name": "Last.fm", "kind": "scheduled", "enabled": True,
         "last_run": None, "last_outcome": None, "last_error": None,
         "consecutive_failures": 0},
    ], "load_errors": {}}
    monkeypatch.setattr("fulcra_collect.cli.send_request", lambda *a, **k: snapshot)
    res = CliRunner().invoke(cli, ["status"])
    assert res.exit_code == 0
    assert "lastfm" in res.output


def test_set_credential_stores_into_the_keychain(collect_home: Path, monkeypatch):
    stored = {}
    monkeypatch.setattr("fulcra_collect.cli.credentials.set_secret",
                        lambda pid, key, val: stored.update({(pid, key): val}))
    res = CliRunner().invoke(cli, ["set-credential", "lastfm", "api-key"],
                             input="SECRET123\n")
    assert res.exit_code == 0
    assert stored == {("lastfm", "api-key"): "SECRET123"}


def test_reset_definition_clears_cache(collect_home: Path, monkeypatch):
    from fulcra_collect import state

    st = state.PluginState(plugin_id="lastfm", definition_id="cached-uuid")
    state.save(st)

    result = CliRunner().invoke(cli, ["plugin", "reset-definition", "lastfm"])
    assert result.exit_code == 0, result.output

    after = state.load("lastfm")
    assert after.definition_id is None


@pytest.mark.parametrize("error,code,message", [
    ("DaemonAlreadyRunning", 75, "daemon startup refused: profile lease busy"),
    ("DaemonLeaseUnavailable", 1, "daemon startup refused: profile lease unavailable"),
])
def test_daemon_lease_refusal_is_static_and_has_distinct_exit(monkeypatch, error, code, message):
    from fulcra_collect import daemon_lease
    class RefusedDaemon:
        def __init__(self, *, launch_menubar):
            pass

        def serve(self):
            raise getattr(daemon_lease, error)("sensitive internal path")
    monkeypatch.setattr("fulcra_collect.cli.Daemon", RefusedDaemon)
    monkeypatch.setattr("fulcra_collect.cli._stderr_is_a_tty", lambda: False)
    result = CliRunner().invoke(cli, ["daemon"])
    assert result.exit_code == code
    assert result.stderr == message + "\n"
    assert "sensitive" not in result.output
    assert "Traceback" not in result.output


def test_daemon_cleanup_failure_has_static_unavailable_cli(collect_home, monkeypatch):
    import errno
    import os
    import stat
    from fulcra_collect import daemon_lease
    real_close = os.close
    collect_home.chmod(0o700)
    class LeaseOnlyDaemon:
        def __init__(self, *, launch_menubar):
            pass

        def serve(self):
            daemon_lease.acquire_daemon_lease(collect_home)
    def failed_acquisition(*args):
        raise OSError(errno.EIO, "sensitive acquisition detail")
    def failed_close(fd):
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
        real_close(fd)
        if regular:
            raise OSError(errno.EIO, "sensitive cleanup detail")
    monkeypatch.setattr("fulcra_collect.cli.Daemon", LeaseOnlyDaemon)
    monkeypatch.setattr("fulcra_collect.cli._stderr_is_a_tty", lambda: False)
    monkeypatch.setattr(daemon_lease.fcntl, "flock", failed_acquisition)
    with monkeypatch.context() as patch:
        patch.setattr(daemon_lease.os, "close", failed_close)
        result = CliRunner().invoke(cli, ["daemon"])
    assert result.exit_code == 1
    assert result.stderr == "daemon startup refused: profile lease unavailable\n"
    assert "sensitive" not in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, OSError)

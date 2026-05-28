"""Tests for the daemon's best-effort menubar launcher (#66).

These are hermetic — no real subprocess.Popen, no real pgrep. The
module's failure-mode contract ("never raise; daemon startup must
survive any menubar launch failure") is exercised by patching the
internals and asserting status() / try_launch_menubar() behavior.
"""
from __future__ import annotations

import subprocess

import pytest

from fulcra_collect import menubar_launcher as ml


def test_is_supported_returns_bool():
    assert isinstance(ml.is_supported(), bool)


def test_status_returns_unsupported_on_non_darwin(monkeypatch):
    monkeypatch.setattr(ml, "is_supported", lambda: False)
    assert ml.status() == "unsupported"


def test_status_returns_running_when_pgrep_matches(monkeypatch):
    monkeypatch.setattr(ml, "is_supported", lambda: True)
    monkeypatch.setattr(ml, "is_running", lambda: True)
    assert ml.status() == "running"


def test_status_returns_not_installed_when_command_missing(monkeypatch):
    monkeypatch.setattr(ml, "is_supported", lambda: True)
    monkeypatch.setattr(ml, "is_running", lambda: False)
    monkeypatch.setattr(ml, "find_menubar_command", lambda: None)
    assert ml.status() == "not_installed"


def test_status_returns_not_running_when_installed_but_quiet(monkeypatch):
    monkeypatch.setattr(ml, "is_supported", lambda: True)
    monkeypatch.setattr(ml, "is_running", lambda: False)
    monkeypatch.setattr(ml, "find_menubar_command", lambda: ["/x/fulcra-menubar"])
    assert ml.status() == "not_running"


def test_try_launch_noops_on_non_darwin(monkeypatch):
    monkeypatch.setattr(ml, "is_supported", lambda: False)
    # Should NOT call Popen; if it did, our stub below would record it.
    called: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: called.append((a, kw)))
    assert ml.try_launch_menubar() is False
    assert called == []


def test_try_launch_skips_when_already_running(monkeypatch):
    monkeypatch.setattr(ml, "is_supported", lambda: True)
    monkeypatch.setattr(ml, "is_running", lambda: True)
    called: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: called.append((a, kw)))
    assert ml.try_launch_menubar(only_if_not_running=True) is True
    assert called == []  # didn't try to launch a duplicate


def test_try_launch_returns_false_when_command_missing(monkeypatch):
    monkeypatch.setattr(ml, "is_supported", lambda: True)
    monkeypatch.setattr(ml, "is_running", lambda: False)
    monkeypatch.setattr(ml, "find_menubar_command", lambda: None)
    called: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: called.append((a, kw)))
    assert ml.try_launch_menubar() is False
    assert called == []  # nothing to launch


def test_try_launch_calls_popen_with_detach(monkeypatch):
    monkeypatch.setattr(ml, "is_supported", lambda: True)
    monkeypatch.setattr(ml, "is_running", lambda: False)
    monkeypatch.setattr(
        ml, "find_menubar_command",
        lambda: ["/usr/local/bin/fulcra-menubar"],
    )
    captured: dict = {}
    class _Fake:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
    monkeypatch.setattr(subprocess, "Popen", _Fake)
    assert ml.try_launch_menubar() is True
    assert captured["argv"] == ["/usr/local/bin/fulcra-menubar"]
    # Detached lifecycle is the whole point — outliving the daemon is
    # the contract callers rely on.
    assert captured["kw"]["start_new_session"] is True
    assert captured["kw"]["close_fds"] is True


def test_try_launch_swallows_popen_failures(monkeypatch):
    """Daemon startup must NEVER be blocked by a menubar launch failure.
    The wide-except in try_launch_menubar is load-bearing."""
    monkeypatch.setattr(ml, "is_supported", lambda: True)
    monkeypatch.setattr(ml, "is_running", lambda: False)
    monkeypatch.setattr(
        ml, "find_menubar_command",
        lambda: ["/usr/local/bin/fulcra-menubar"],
    )
    def _boom(*a, **kw):
        raise OSError("simulated subprocess failure")
    monkeypatch.setattr(subprocess, "Popen", _boom)
    # Should not raise; should return False.
    assert ml.try_launch_menubar() is False


def test_menubar_command_display_returns_path_when_command_resolved(monkeypatch):
    monkeypatch.setattr(
        ml, "find_menubar_command",
        lambda: ["/Users/me/.local/bin/fulcra-menubar"],
    )
    assert ml.menubar_command_display() == "/Users/me/.local/bin/fulcra-menubar"


def test_menubar_command_display_joins_argv_for_module_invocation(monkeypatch):
    monkeypatch.setattr(
        ml, "find_menubar_command",
        lambda: ["/opt/python", "-m", "fulcra_menubar"],
    )
    assert ml.menubar_command_display() == "/opt/python -m fulcra_menubar"


def test_menubar_command_display_returns_none_when_not_installed(monkeypatch):
    monkeypatch.setattr(ml, "find_menubar_command", lambda: None)
    assert ml.menubar_command_display() is None


@pytest.mark.skipif(
    not ml.is_supported(),
    reason="is_running uses pgrep which exists only on POSIX",
)
def test_is_running_returns_false_when_pgrep_has_no_match(monkeypatch):
    """On the test machine, a fulcra-menubar process MAY genuinely be
    running. We can't assume either way — instead we stub pgrep to
    always return no-match and confirm we report False."""
    class _Result:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _Result(),
    )
    assert ml.is_running() is False

"""Tests for `fulcra-collect doctor`.

Each test covers one of the three core failure modes the spec calls out:
  (a) fulcra CLI not found → FAIL + hint
  (b) CLI found but auth fails (signed out) → WARN
  (c) daemon control socket not reachable → FAIL

We use CliRunner to invoke the command end-to-end, then assert both the
status label and the fix-hint substring appear in the output.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fulcra_collect.cli import cli


# ---------------------------------------------------------------------------
# Helpers / shared mocks
# ---------------------------------------------------------------------------

def _make_cli_ok(cli_path: str = "/usr/local/bin/fulcra") -> object:
    """A subprocess.CompletedProcess that looks like a successful token print."""
    r = MagicMock()
    r.returncode = 0
    r.stdout = "tok_abc123\n"
    r.stderr = ""
    return r


def _make_cli_fail() -> object:
    """A subprocess.CompletedProcess that looks like an auth failure."""
    r = MagicMock()
    r.returncode = 1
    r.stdout = ""
    r.stderr = "not authenticated"
    return r


def _daemon_reply_ok() -> dict:
    return {"ok": True, "plugins": [{"id": "lastfm", "kind": "scheduled",
                                      "enabled": True, "last_outcome": None}],
            "load_errors": {}}


# ---------------------------------------------------------------------------
# (a) CLI not found — should print FAIL + hint
# ---------------------------------------------------------------------------

def test_doctor_cli_not_found_prints_fail_and_hint(collect_home: Path, monkeypatch):
    """When _find_fulcra_cli returns None, doctor prints FAIL and the install hint."""
    monkeypatch.setattr("fulcra_collect.credentials._find_fulcra_cli", lambda: None)

    # Daemon must be reachable so it doesn't add extra FAIL lines that obscure the one
    # we're testing. Control socket FAIL is tested separately below.
    monkeypatch.setattr("fulcra_collect.control.send_request",
                        lambda *a, **k: _daemon_reply_ok())

    # web-token must exist to avoid an unrelated FAIL
    (collect_home / "web-token").write_text("tok\n")

    # bearer token absent → WARN (not FAIL) for check 7
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret", lambda k: None)

    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])

    assert "FAIL" in result.output
    assert "uv tool install fulcra-api" in result.output
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# (b) CLI found but not signed in — should print WARN
# ---------------------------------------------------------------------------

def test_doctor_cli_not_signed_in_prints_warn(collect_home: Path, monkeypatch):
    """When the CLI is found but returns non-zero, doctor shows WARN with auth hint."""
    monkeypatch.setattr(
        "fulcra_collect.credentials._find_fulcra_cli",
        lambda: "/usr/local/bin/fulcra",
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: _make_cli_fail(),
    )

    # Keep other checks green so the WARN from check 2 is distinguishable.
    monkeypatch.setattr("fulcra_collect.control.send_request",
                        lambda *a, **k: _daemon_reply_ok())
    (collect_home / "web-token").write_text("tok\n")
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret", lambda k: None)

    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])

    assert "WARN" in result.output
    assert "fulcra auth login" in result.output
    # CLI found so there should be no FAIL on check 1 — check 2 is a WARN, not a FAIL.
    # Exit code could be 0 (only WARNs/no FAILs) depending on other checks.
    # The important assertion is the WARN text.


# ---------------------------------------------------------------------------
# (c) Daemon control socket not reachable — should print FAIL
# ---------------------------------------------------------------------------

def test_doctor_daemon_unreachable_prints_fail_and_hint(collect_home: Path, monkeypatch):
    """When the daemon socket is missing, doctor prints FAIL with a startup hint."""
    monkeypatch.setattr(
        "fulcra_collect.credentials._find_fulcra_cli",
        lambda: "/usr/local/bin/fulcra",
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: _make_cli_ok(),
    )
    monkeypatch.setattr(
        "fulcra_collect.control.send_request",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("not running")),
    )
    (collect_home / "web-token").write_text("tok\n")
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret", lambda k: None)

    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])

    assert "FAIL" in result.output
    # Hint varies based on whether the plist exists; the key word is "install"
    assert "install" in result.output.lower()
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# (d) Happy path — all checks pass → exit 0
# ---------------------------------------------------------------------------

def test_doctor_all_ok_exits_zero(collect_home: Path, monkeypatch):
    """When everything checks out, doctor exits 0 and shows only OK lines."""
    monkeypatch.setattr(
        "fulcra_collect.credentials._find_fulcra_cli",
        lambda: "/usr/local/bin/fulcra",
    )

    # subprocess.run is called for both the CLI auth check AND launchctl; we
    # need to return an OK result for both.
    cli_ok = _make_cli_ok()
    launchctl_ok = MagicMock()
    launchctl_ok.returncode = 0
    launchctl_ok.stdout = "..."
    launchctl_ok.stderr = ""

    call_count = [0]

    def _fake_run(cmd, **kw):
        call_count[0] += 1
        if "launchctl" in cmd[0]:
            return launchctl_ok
        return cli_ok

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr("fulcra_collect.control.send_request",
                        lambda *a, **k: _daemon_reply_ok())
    (collect_home / "web-token").write_text("tok\n")

    # Fake bearer token + httpx response
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret",
                        lambda k: "fake-bearer-tok")

    fake_resp = MagicMock()
    fake_resp.status_code = 200

    import sys
    fake_httpx = MagicMock()
    fake_httpx.get.return_value = fake_resp
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    # Pretend plist exists so the launchd check is exercised.
    from fulcra_collect import service_manager
    monkeypatch.setattr(service_manager, "launchd_plist_path",
                        lambda: collect_home / "fake.plist")
    (collect_home / "fake.plist").write_text("<plist/>")

    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])

    assert "FAIL" not in result.output
    assert result.exit_code == 0

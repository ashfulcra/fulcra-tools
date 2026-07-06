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


def _make_data_updates_ok() -> object:
    """A CompletedProcess that looks like `fulcra data-updates "1 hour"`.
    file_changes deliberately carries a sentinel — doctor must never print it."""
    r = MagicMock()
    r.returncode = 0
    r.stdout = ('{"data_types": {"HeartRate": 795, "StepCount": 306}, '
                '"file_changes": [{"path": "NEVER-PRINT-THIS"}]}')
    r.stderr = ""
    return r


def _dispatch_probe_ok(cmd) -> object:
    """Route a fulcra subprocess call to the right OK-shaped fake: JSON for
    the data-liveness probe, a plain rc-0 result for auth/--help probes."""
    if "data-updates" in cmd and "--help" not in cmd:
        return _make_data_updates_ok()
    return _make_cli_ok()


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

    # subprocess.run is called for the CLI auth check, the file/data-updates
    # feature probes, the data-liveness probe AND launchctl; return an OK
    # result shaped correctly for each.
    launchctl_ok = MagicMock()
    launchctl_ok.returncode = 0
    launchctl_ok.stdout = "..."
    launchctl_ok.stderr = ""

    call_count = [0]

    def _fake_run(cmd, **kw):
        call_count[0] += 1
        if "launchctl" in cmd[0]:
            return launchctl_ok
        return _dispatch_probe_ok(cmd)

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


def test_doctor_api_health_respects_api_base_override(collect_home: Path,
                                                      monkeypatch):
    """P3 #18: the doctor's API-health probe must hit
    fulcra_common.DEFAULT_BASE_URL (which honors FULCRA_API_BASE), not a
    hardcoded prod URL."""
    monkeypatch.setattr("fulcra_common.DEFAULT_BASE_URL",
                        "https://fulcra.test")
    monkeypatch.setattr("fulcra_collect.credentials._find_fulcra_cli",
                        lambda: "/usr/local/bin/fulcra")
    monkeypatch.setattr("subprocess.run",
                        lambda cmd, **kw: _dispatch_probe_ok(cmd))
    monkeypatch.setattr("fulcra_collect.control.send_request",
                        lambda *a, **k: _daemon_reply_ok())
    (collect_home / "web-token").write_text("tok\n")
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret",
                        lambda k: "fake-bearer-tok")

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    import sys
    fake_httpx = MagicMock()
    fake_httpx.get.return_value = fake_resp
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    runner = CliRunner()
    runner.invoke(cli, ["doctor"])

    assert fake_httpx.get.called, "API-health probe never fired"
    (url,), _kwargs = fake_httpx.get.call_args
    assert url == "https://fulcra.test/user/v1alpha1/annotation"


# ---------------------------------------------------------------------------
# (e) data-updates adoption rows: file group, version floor (feature probe —
#     there is no `fulcra --version`), and data liveness.
# ---------------------------------------------------------------------------

def _quiet_other_checks(collect_home: Path, monkeypatch):
    """Keep the non-CLI checks green so the row under test is the only
    FAIL candidate."""
    monkeypatch.setattr("fulcra_collect.control.send_request",
                        lambda *a, **k: _daemon_reply_ok())
    (collect_home / "web-token").write_text("tok\n")
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret",
                        lambda k: None)


def test_doctor_file_group_missing_fails_with_upgrade_hint(collect_home: Path,
                                                           monkeypatch):
    """An old fulcra-api build without the file group → FAIL + upgrade hint."""
    monkeypatch.setattr("fulcra_collect.credentials._find_fulcra_cli",
                        lambda: "/usr/local/bin/fulcra")
    _quiet_other_checks(collect_home, monkeypatch)

    def _fake_run(cmd, **kw):
        if "file" in cmd:
            r = MagicMock()
            r.returncode = 2
            r.stdout = ""
            r.stderr = "Error: No such command 'file'."
            return r
        return _dispatch_probe_ok(cmd)

    monkeypatch.setattr("subprocess.run", _fake_run)
    result = CliRunner().invoke(cli, ["doctor"])
    assert "fulcra CLI file group" in result.output
    assert "uv tool install --upgrade fulcra-api" in result.output
    assert result.exit_code == 1


def test_doctor_data_updates_probe_passes_when_command_exists(collect_home: Path,
                                                              monkeypatch):
    """`fulcra data-updates --help` exiting 0 implies fulcra-api >= 0.1.35
    (feature probe — the CLI has no --version); the row reads OK."""
    monkeypatch.setattr("fulcra_collect.credentials._find_fulcra_cli",
                        lambda: "/usr/local/bin/fulcra")
    _quiet_other_checks(collect_home, monkeypatch)
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _dispatch_probe_ok(cmd))
    result = CliRunner().invoke(cli, ["doctor"])
    assert "fulcra CLI data-updates (>=0.1.35)" in result.output
    line = [ln for ln in result.output.splitlines()
            if "data-updates (>=0.1.35)" in ln][0]
    assert "[OK]" in line


def test_doctor_data_updates_probe_fails_on_old_cli(collect_home: Path,
                                                    monkeypatch):
    """A pre-0.1.35 CLI (no data-updates command) → FAIL + upgrade hint on
    the version-floor row AND the liveness row is skipped as FAIL."""
    monkeypatch.setattr("fulcra_collect.credentials._find_fulcra_cli",
                        lambda: "/usr/local/bin/fulcra")
    _quiet_other_checks(collect_home, monkeypatch)

    def _fake_run(cmd, **kw):
        if "data-updates" in cmd:
            r = MagicMock()
            r.returncode = 2
            r.stdout = ""
            r.stderr = "Error: No such command 'data-updates'."
            return r
        return _dispatch_probe_ok(cmd)

    monkeypatch.setattr("subprocess.run", _fake_run)
    result = CliRunner().invoke(cli, ["doctor"])
    assert "uv tool install --upgrade fulcra-api" in result.output
    liveness = [ln for ln in result.output.splitlines()
                if "data liveness" in ln][0]
    assert "[FAIL]" in liveness and "skipped" in liveness
    assert result.exit_code == 1


def test_doctor_data_liveness_summarises_data_types_never_file_changes(
        collect_home: Path, monkeypatch):
    """PASS prints a compact data_types summary; the (potentially huge)
    file_changes array must never reach the output."""
    monkeypatch.setattr("fulcra_collect.credentials._find_fulcra_cli",
                        lambda: "/usr/local/bin/fulcra")
    _quiet_other_checks(collect_home, monkeypatch)
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _dispatch_probe_ok(cmd))
    result = CliRunner().invoke(cli, ["doctor"])
    liveness = [ln for ln in result.output.splitlines()
                if "data liveness" in ln][0]
    assert "[OK]" in liveness
    # 795 + 306 records across 2 data types (from _make_data_updates_ok).
    assert "2 data type(s)" in liveness
    assert "1101 record(s)" in liveness
    assert "NEVER-PRINT-THIS" not in result.output


def test_doctor_data_liveness_failure_surfaces_stderr_tail(collect_home: Path,
                                                           monkeypatch):
    """A failing liveness probe (e.g. signed out, API down) FAILs with the
    stderr tail so the user sees the server's actual complaint."""
    monkeypatch.setattr("fulcra_collect.credentials._find_fulcra_cli",
                        lambda: "/usr/local/bin/fulcra")
    _quiet_other_checks(collect_home, monkeypatch)

    def _fake_run(cmd, **kw):
        if "data-updates" in cmd and "--help" not in cmd:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "Error: HTTP Error 500: Internal Server Error"
            return r
        return _dispatch_probe_ok(cmd)

    monkeypatch.setattr("subprocess.run", _fake_run)
    result = CliRunner().invoke(cli, ["doctor"])
    liveness = [ln for ln in result.output.splitlines()
                if "data liveness" in ln][0]
    assert "[FAIL]" in liveness
    assert "HTTP Error 500" in liveness
    assert result.exit_code == 1


def test_doctor_data_liveness_warns_not_fails_when_signed_out(
        collect_home: Path, monkeypatch):
    """Signed-out is already reported (as WARN) by the 'fulcra CLI reachable'
    check; the liveness row must not pile a second hard FAIL on top of it —
    it WARNs with a sign-in hint instead. Doctor run before first sign-in
    (a normal onboarding state) should not look like a broken install."""
    monkeypatch.setattr("fulcra_collect.credentials._find_fulcra_cli",
                        lambda: "/usr/local/bin/fulcra")
    _quiet_other_checks(collect_home, monkeypatch)

    def _fake_run(cmd, **kw):
        if "print-access-token" in cmd:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "Error: not logged in"
            return r
        if "data-updates" in cmd and "--help" not in cmd:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "Error: HTTP Error 401: Unauthorized"
            return r
        return _dispatch_probe_ok(cmd)

    monkeypatch.setattr("subprocess.run", _fake_run)
    result = CliRunner().invoke(cli, ["doctor"])
    liveness = [ln for ln in result.output.splitlines()
                if "data liveness" in ln][0]
    assert "[WARN]" in liveness
    assert "not signed in" in liveness
    assert "401" not in liveness  # the raw stderr FAIL path was not taken

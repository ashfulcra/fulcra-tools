"""Tests for the menubar's daemon lifecycle controller.

Hermetic: no real launchctl calls, no SMAppService calls. Tests pass
on Linux CI as well as macOS — the SMAppService-touching branches are
gated on ``_SM_AVAILABLE`` and exercised via a fake injected into the
module's lookup.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fulcra_menubar import daemon_lifecycle as dl
from fulcra_menubar.daemon_client import DaemonUnavailable


# ── is_installed() ───────────────────────────────────────────────────────────

def test_is_installed_true_when_plist_exists(tmp_path, monkeypatch):
    plist = tmp_path / "com.fulcra.collect.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(dl, "plist_path", lambda: plist)
    assert dl.is_installed() is True


def test_is_installed_false_when_plist_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "plist_path", lambda: tmp_path / "nope.plist")
    assert dl.is_installed() is False


# ── is_running() ──────────────────────────────────────────────────────────────

class _StubClient:
    """Minimal DaemonClient stand-in: ``version()`` returns whatever
    the fixture wires up, or raises DaemonUnavailable."""
    def __init__(self, *, reply=None, raise_=None):
        self._reply = reply
        self._raise = raise_

    def version(self):
        if self._raise is not None:
            raise self._raise
        return self._reply


def test_is_running_true_with_pid():
    client = _StubClient(reply={"ok": True, "daemon_version": "0.1", "daemon_pid": 12345})
    assert dl.is_running(client=client) == (True, 12345)


def test_is_running_true_but_pid_missing_on_old_daemon():
    # An older daemon that doesn't yet include daemon_pid still reports as
    # running — we just can't show the PID in the menu line.
    client = _StubClient(reply={"ok": True, "daemon_version": "0.1"})
    assert dl.is_running(client=client) == (True, None)


def test_is_running_false_when_socket_missing():
    client = _StubClient(raise_=DaemonUnavailable("no socket"))
    assert dl.is_running(client=client) == (False, None)


def test_is_running_false_when_reply_not_ok():
    client = _StubClient(reply={"ok": False, "error": "boom"})
    assert dl.is_running(client=client) == (False, None)


# ── status() ──────────────────────────────────────────────────────────────────

@pytest.fixture
def _no_sm(monkeypatch):
    """Pretend SMAppService is unavailable so login_item_status() returns
    'unavailable' and the test exercises only the installed/running axes."""
    monkeypatch.setattr(dl, "_SM_AVAILABLE", False)
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: None)
    yield


def test_status_running_overrides_installed(tmp_path, monkeypatch, _no_sm):
    monkeypatch.setattr(dl, "plist_path", lambda: tmp_path / "no.plist")
    client = _StubClient(reply={"ok": True, "daemon_pid": 1})
    assert dl.status(client=client) == "running"


def test_status_stopped_when_installed_but_no_daemon(tmp_path, monkeypatch, _no_sm):
    plist = tmp_path / "com.fulcra.collect.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(dl, "plist_path", lambda: plist)
    client = _StubClient(raise_=DaemonUnavailable("no socket"))
    assert dl.status(client=client) == "stopped"


def test_status_not_installed(tmp_path, monkeypatch, _no_sm):
    monkeypatch.setattr(dl, "plist_path", lambda: tmp_path / "no.plist")
    client = _StubClient(raise_=DaemonUnavailable("no socket"))
    assert dl.status(client=client) == "not_installed"


def test_status_needs_approval_takes_priority(tmp_path, monkeypatch):
    # SM says requires_approval — that wins over any other axis because
    # the user has an explicit action to take in System Settings.
    monkeypatch.setattr(dl, "login_item_status", lambda: "needs_approval")
    monkeypatch.setattr(dl, "plist_path", lambda: tmp_path / "no.plist")
    client = _StubClient(reply={"ok": True, "daemon_pid": 99})
    assert dl.status(client=client) == "needs_approval"


# ── start / stop / restart → launchctl arg shapes ────────────────────────────

def test_start_calls_launchctl_kickstart(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_launchctl(*args):
        calls.append(args)
        return (0, "")

    monkeypatch.setattr(dl, "_launchctl", fake_launchctl)
    dl.start()
    # First call should be kickstart against the user-domain label.
    assert calls[0][0] == "kickstart"
    assert calls[0][1].startswith("gui/")
    assert calls[0][1].endswith(f"/{dl.LAUNCHD_LABEL}")


def test_start_bootstraps_then_kickstarts_on_first_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(dl, "plist_path", lambda: tmp_path / "x.plist")
    calls: list[tuple[str, ...]] = []
    # First kickstart fails (service not loaded), bootstrap succeeds,
    # second kickstart succeeds.
    outcomes = iter([(1, "no such service"), (0, ""), (0, "")])

    def fake_launchctl(*args):
        calls.append(args)
        return next(outcomes)

    monkeypatch.setattr(dl, "_launchctl", fake_launchctl)
    dl.start()
    assert [c[0] for c in calls] == ["kickstart", "bootstrap", "kickstart"]
    # The bootstrap should reference the plist path.
    assert str(tmp_path / "x.plist") in calls[1]


def test_start_raises_on_unrecoverable_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(dl, "plist_path", lambda: tmp_path / "x.plist")

    def fake_launchctl(*args):
        return (1, "permission denied")

    monkeypatch.setattr(dl, "_launchctl", fake_launchctl)
    with pytest.raises(dl.DaemonLifecycleError) as exc:
        dl.start()
    assert "permission denied" in str(exc.value)


def test_stop_calls_bootout(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_launchctl(*args):
        calls.append(args)
        return (0, "")

    monkeypatch.setattr(dl, "_launchctl", fake_launchctl)
    dl.stop()
    assert calls[0][0] == "bootout"
    assert calls[0][1].startswith("gui/")
    assert calls[0][1].endswith(f"/{dl.LAUNCHD_LABEL}")


def test_stop_treats_not_loaded_as_success(monkeypatch):
    def fake_launchctl(*args):
        return (3, "Could not find service \"com.fulcra.collect\" in domain")

    monkeypatch.setattr(dl, "_launchctl", fake_launchctl)
    # Must NOT raise — the user wanted it stopped, it's already stopped.
    dl.stop()


def test_stop_raises_on_real_failure(monkeypatch):
    def fake_launchctl(*args):
        return (1, "operation not permitted")

    monkeypatch.setattr(dl, "_launchctl", fake_launchctl)
    with pytest.raises(dl.DaemonLifecycleError):
        dl.stop()


def test_restart_calls_stop_then_start(monkeypatch):
    calls: list[str] = []

    def fake_stop():
        calls.append("stop")

    def fake_start():
        calls.append("start")

    monkeypatch.setattr(dl, "stop", fake_stop)
    monkeypatch.setattr(dl, "start", fake_start)
    dl.restart()
    assert calls == ["stop", "start"]


def test_restart_continues_to_start_if_stop_fails(monkeypatch):
    def fake_stop():
        raise dl.DaemonLifecycleError("stop boom")

    calls: list[str] = []

    def fake_start():
        calls.append("start")

    monkeypatch.setattr(dl, "stop", fake_stop)
    monkeypatch.setattr(dl, "start", fake_start)
    # restart() should swallow the stop error and still call start().
    dl.restart()
    assert calls == ["start"]


# ── login-item registration: SM-not-available path ─────────────────────────

def test_login_item_status_unavailable_off_macos(monkeypatch):
    monkeypatch.setattr(dl, "_SM_AVAILABLE", False)
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: None)
    assert dl.login_item_status() == "unavailable"


def test_register_login_item_raises_without_sm(monkeypatch):
    monkeypatch.setattr(dl, "_SM_AVAILABLE", False)
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: None)
    with pytest.raises(dl.DaemonLifecycleError):
        dl.register_login_item()


def test_unregister_login_item_raises_without_sm(monkeypatch):
    monkeypatch.setattr(dl, "_SM_AVAILABLE", False)
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: None)
    with pytest.raises(dl.DaemonLifecycleError):
        dl.unregister_login_item()


# ── login-item registration: with a fake SMAppService ───────────────────────

class _FakeError:
    def __init__(self, msg):
        self._msg = msg

    def localizedDescription(self):
        return self._msg


class _FakeSMService:
    """Stub matching the bits of SMAppService that daemon_lifecycle calls."""

    def __init__(self, *, status_value, register_ok=True, register_err=None,
                 unregister_ok=True, unregister_err=None):
        self._status = status_value
        self._register_ok = register_ok
        self._register_err = register_err
        self._unregister_ok = unregister_ok
        self._unregister_err = unregister_err
        self.register_called = 0
        self.unregister_called = 0

    def status(self):
        return self._status

    def registerAndReturnError_(self, _err_ptr):
        self.register_called += 1
        return (self._register_ok, self._register_err)

    def unregisterAndReturnError_(self, _err_ptr):
        self.unregister_called += 1
        return (self._unregister_ok, self._unregister_err)


def test_login_item_status_enabled_via_fake_sm(monkeypatch):
    fake = _FakeSMService(status_value=dl.SM_STATUS_ENABLED)
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: fake)
    monkeypatch.setattr(dl, "_SM_AVAILABLE", True)
    assert dl.login_item_status() == "enabled"


def test_login_item_status_needs_approval_via_fake_sm(monkeypatch):
    fake = _FakeSMService(status_value=dl.SM_STATUS_REQUIRES_APPROVAL)
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: fake)
    monkeypatch.setattr(dl, "_SM_AVAILABLE", True)
    assert dl.login_item_status() == "needs_approval"


def test_register_login_item_calls_registerAndReturnError(monkeypatch):
    fake = _FakeSMService(status_value=dl.SM_STATUS_NOT_REGISTERED)
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: fake)
    monkeypatch.setattr(dl, "_SM_AVAILABLE", True)
    dl.register_login_item()
    assert fake.register_called == 1


def test_register_login_item_raises_on_sm_failure(monkeypatch):
    fake = _FakeSMService(
        status_value=dl.SM_STATUS_NOT_REGISTERED,
        register_ok=False, register_err=_FakeError("user denied"),
    )
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: fake)
    monkeypatch.setattr(dl, "_SM_AVAILABLE", True)
    with pytest.raises(dl.DaemonLifecycleError) as exc:
        dl.register_login_item()
    assert "user denied" in str(exc.value)


def test_unregister_login_item_calls_sm(monkeypatch):
    fake = _FakeSMService(status_value=dl.SM_STATUS_ENABLED)
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: fake)
    monkeypatch.setattr(dl, "_SM_AVAILABLE", True)
    dl.unregister_login_item()
    assert fake.unregister_called == 1


# ── install / uninstall ────────────────────────────────────────────────────

def test_install_writes_plist_and_registers_login_item(tmp_path, monkeypatch):
    written: dict[str, Path] = {}

    def fake_install(*, executable):
        path = tmp_path / "com.fulcra.collect.plist"
        path.write_text(f"<plist exe={executable!r}/>")
        written["path"] = path
        return path

    from fulcra_collect import service_manager as _sm
    monkeypatch.setattr(_sm, "install", fake_install)

    fake = _FakeSMService(status_value=dl.SM_STATUS_NOT_REGISTERED)
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: fake)
    monkeypatch.setattr(dl, "_SM_AVAILABLE", True)
    monkeypatch.setattr(dl, "plist_path", lambda: tmp_path / "com.fulcra.collect.plist")

    path = dl.install()
    assert path == written["path"]
    assert fake.register_called == 1


def test_install_tolerates_sm_failure(tmp_path, monkeypatch):
    """Plist still gets written even if SMAppService refuses — user can
    still load it manually with launchctl."""
    def fake_install(*, executable):
        path = tmp_path / "com.fulcra.collect.plist"
        path.write_text("<plist/>")
        return path

    from fulcra_collect import service_manager as _sm
    monkeypatch.setattr(_sm, "install", fake_install)

    fake = _FakeSMService(
        status_value=dl.SM_STATUS_NOT_REGISTERED,
        register_ok=False, register_err=_FakeError("approval pending"),
    )
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: fake)
    monkeypatch.setattr(dl, "_SM_AVAILABLE", True)
    monkeypatch.setattr(dl, "plist_path", lambda: tmp_path / "com.fulcra.collect.plist")

    # Should NOT raise — install is best-effort on the SM half.
    path = dl.install()
    assert path.exists()


def test_uninstall_removes_plist_and_unregisters(tmp_path, monkeypatch):
    plist = tmp_path / "com.fulcra.collect.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(dl, "plist_path", lambda: plist)

    fake = _FakeSMService(status_value=dl.SM_STATUS_ENABLED)
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: fake)
    monkeypatch.setattr(dl, "_SM_AVAILABLE", True)

    dl.uninstall()
    assert not plist.exists()
    assert fake.unregister_called == 1


def test_uninstall_tolerates_missing_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "plist_path", lambda: tmp_path / "missing.plist")
    fake = _FakeSMService(status_value=dl.SM_STATUS_NOT_REGISTERED)
    monkeypatch.setattr(dl, "_sm_agent_service", lambda: fake)
    monkeypatch.setattr(dl, "_SM_AVAILABLE", True)
    # No error even though there's nothing to delete.
    dl.uninstall()

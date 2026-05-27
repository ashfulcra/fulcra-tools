"""Daemon lifecycle control via SMAppService.

This is the user-visible Start / Stop / Restart / Open-at-Login surface
of the menubar app. Under the hood it uses macOS SMAppService (10.15+
for `.daemon`, 13+ for `.agent`) instead of writing launchd plists
ourselves — so registration is app-bundle-aware, the "Open at Login"
state lives in System Settings where users expect it, and uninstalling
the menubar app cleans up the daemon registration too.

We still depend on `~/Library/LaunchAgents/com.fulcra.collect.plist`
existing (written by `fulcra-collect install` or by us via the
PlistWriter helper below) — SMAppService.agent(plistName:) only
references an already-deployed plist. The CLI install path and the
menubar's install-from-here path both end up at the same plist.

Module surface
──────────────
- ``is_installed()`` — has the launchd plist been written?
- ``is_running()`` — is the daemon currently answering on the control socket?
  Returns ``(running, pid)``; ``pid`` is ``None`` when not running or when
  the running daemon predates the ``daemon_pid`` field on /version replies.
- ``status()`` — composes the two into a single high-level state.
- ``install()`` — writes the plist (via fulcra_collect.service_manager,
  the existing CLI path) AND registers it with SMAppService so the
  "Open at Login" toggle reflects it.
- ``uninstall()`` — unregister via SMAppService and remove the plist.
- ``start()`` / ``stop()`` / ``restart()`` — shell out to launchctl
  against the LAUNCHD_LABEL.  Synchronous; ≤2s; raises
  ``DaemonLifecycleError`` on failure so the caller can post a
  notification with the underlying message.
- ``register_login_item()`` / ``unregister_login_item()`` /
  ``login_item_status()`` — Open-at-Login toggle plumbing.

All SMAppService-touching code paths are gated on ``_SM_AVAILABLE``
and become no-ops on non-macOS so the tests run on Linux CI too.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from .daemon_client import DaemonClient, DaemonUnavailable

logger = logging.getLogger("fulcra_menubar.daemon_lifecycle")

LAUNCHD_LABEL = "com.fulcra.collect"
PLIST_NAME = f"{LAUNCHD_LABEL}.plist"

# Lazy / graceful PyObjC import — keeps the module importable (and the
# tests runnable) on non-macOS hosts.
try:
    from ServiceManagement import SMAppService  # type: ignore[import-not-found]
    _SM_AVAILABLE = True
except ImportError:  # pragma: no cover — non-macOS path
    SMAppService = None  # type: ignore[assignment]
    _SM_AVAILABLE = False


# SMAppServiceStatus enum values (from the macOS SDK headers).  Mirrored
# here as plain ints so we can compare without importing the type — PyObjC
# bridges the enum as a plain integer anyway.
SM_STATUS_NOT_REGISTERED = 0
SM_STATUS_ENABLED = 1
SM_STATUS_REQUIRES_APPROVAL = 2
SM_STATUS_NOT_FOUND = 3


class DaemonLifecycleError(RuntimeError):
    """Raised by start/stop/restart/install/uninstall when the underlying
    operation fails.  The message is user-facing: callers (the menubar's
    notification surface) forward it verbatim to the macOS notification
    centre."""


# ---- Plist path resolver ---------------------------------------------------

def plist_path() -> Path:
    """Path the launchd plist is expected to live at.

    Indirected so tests can monkeypatch this module-level function and
    point ``is_installed()`` at a tmp_path file."""
    return Path.home() / "Library" / "LaunchAgents" / PLIST_NAME


# ---- State queries ---------------------------------------------------------

def is_installed() -> bool:
    """True when the launchd plist file exists on disk."""
    return plist_path().exists()


def is_running(*, client: DaemonClient | None = None) -> tuple[bool, int | None]:
    """Probe the daemon over the control socket.

    Returns ``(running, pid)``.  ``pid`` is ``None`` when:
      - the daemon is not running, OR
      - the daemon is running but predates the ``daemon_pid`` field
        on /version replies (treat as "running, unknown PID").

    Uses ``DaemonClient.version()`` rather than ``.status()`` because
    /version is cheap and returns the PID we need anyway.
    """
    c = client if client is not None else DaemonClient(timeout=1.0)
    try:
        reply = c.version()
    except DaemonUnavailable:
        return (False, None)
    if not reply.get("ok"):
        return (False, None)
    return (True, reply.get("daemon_pid"))


StatusLiteral = Literal["running", "stopped", "not_installed", "needs_approval"]


def status(*, client: DaemonClient | None = None) -> StatusLiteral:
    """Compose ``is_installed`` + ``is_running`` + the SMAppService
    login-item status into a single high-level state.

    Ordering matters:
      - If SMAppService says ``requiresApproval`` (user denied / pending
        Login Items approval), surface that first — the user needs to act.
      - Else if the daemon is responding, it's running (regardless of
        whether the plist was written by us or by the CLI).
      - Else if the plist exists but no daemon, "stopped".
      - Else "not_installed".
    """
    if login_item_status() == "needs_approval":
        return "needs_approval"
    running, _pid = is_running(client=client)
    if running:
        return "running"
    if is_installed():
        return "stopped"
    return "not_installed"


# ---- Login-Item (SMAppService) registration ------------------------------

def _sm_agent_service():
    """Return the SMAppService instance for our plist, or None on non-macOS.

    Constructed on-demand so module import doesn't reach into Cocoa."""
    if not _SM_AVAILABLE:
        return None
    return SMAppService.agentServiceWithPlistName_(PLIST_NAME)


def login_item_status() -> Literal["enabled", "disabled", "needs_approval",
                                    "not_found", "unavailable"]:
    """Query SMAppService for the current "Open at Login" state.

    Returns ``"unavailable"`` on non-macOS or when ServiceManagement
    isn't loadable (test environments, Linux CI).  The menubar treats
    that as "hide the toggle entirely."""
    svc = _sm_agent_service()
    if svc is None:
        return "unavailable"
    raw = svc.status()
    if raw == SM_STATUS_ENABLED:
        return "enabled"
    if raw == SM_STATUS_REQUIRES_APPROVAL:
        return "needs_approval"
    if raw == SM_STATUS_NOT_FOUND:
        return "not_found"
    return "disabled"


def register_login_item() -> None:
    """Register the daemon's plist with launchd via SMAppService so it
    autostarts on login.

    First-time call triggers a one-time macOS approval dialog asking the
    user to allow "Fulcra Collect" as a Login Item.  If they deny, the
    next status() call will return ``"needs_approval"`` and the toggle
    stays off until they enable it manually in System Settings → General
    → Login Items.

    Raises DaemonLifecycleError on any SMAppService failure."""
    svc = _sm_agent_service()
    if svc is None:
        raise DaemonLifecycleError(
            "Login-Item registration requires macOS 13 or later."
        )
    ok, err = svc.registerAndReturnError_(None)
    if not ok:
        raise DaemonLifecycleError(
            f"Couldn't register daemon as Login Item: "
            f"{err.localizedDescription() if err is not None else 'unknown error'}"
        )


def unregister_login_item() -> None:
    """Remove the daemon from Login Items (does NOT delete the plist).

    Raises DaemonLifecycleError on any SMAppService failure."""
    svc = _sm_agent_service()
    if svc is None:
        raise DaemonLifecycleError(
            "Login-Item registration requires macOS 13 or later."
        )
    ok, err = svc.unregisterAndReturnError_(None)
    if not ok:
        raise DaemonLifecycleError(
            f"Couldn't unregister daemon: "
            f"{err.localizedDescription() if err is not None else 'unknown error'}"
        )


# ---- Install / Uninstall --------------------------------------------------

def install() -> Path:
    """Write the launchd plist via fulcra_collect.service_manager.install
    (the same path `fulcra-collect install` uses), then register it as a
    Login Item via SMAppService.

    SMAppService registration is best-effort: if it fails (e.g. user is
    on macOS 12), the plist is still written and the user can load it
    manually with ``launchctl load``.  The error is logged but install()
    returns successfully — the daemon is installable, just not
    autostart-on-login until they're on a newer OS.
    """
    from fulcra_collect import service_manager as _sm
    exe = shutil.which("fulcra-collect") or "fulcra-collect"
    path = _sm.install(executable=exe)
    if _SM_AVAILABLE:
        try:
            register_login_item()
        except DaemonLifecycleError:
            logger.warning(
                "plist written at %s but Login-Item registration failed; "
                "user can still `launchctl load` it manually",
                path, exc_info=True,
            )
    return path


def uninstall() -> None:
    """Unregister from Login Items and remove the plist.

    Best-effort on both halves — a missing plist or a not-registered
    SMAppService is silently OK; only a hard error (permission denied on
    the plist, SMAppService refusal mid-call) raises."""
    if _SM_AVAILABLE:
        try:
            unregister_login_item()
        except DaemonLifecycleError as exc:
            logger.warning("unregister during uninstall failed: %s", exc)
    p = plist_path()
    if p.exists():
        try:
            p.unlink()
        except OSError as exc:
            raise DaemonLifecycleError(
                f"Couldn't remove plist at {p}: {exc}"
            ) from exc


# ---- Start / Stop / Restart -----------------------------------------------

def _launchctl(*args: str) -> tuple[int, str]:
    """Run ``launchctl <args>``; return (returncode, combined stdout+stderr).

    Indirected through a single helper so tests can monkeypatch one
    place to record the exact arg list, and so the timeout/text settings
    stay consistent across start/stop/restart."""
    proc = subprocess.run(
        ["launchctl", *args],
        capture_output=True, text=True, timeout=10,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def start() -> None:
    """Start the daemon. Uses ``launchctl kickstart`` against the
    user-domain label — works whether the daemon is loaded but stopped
    or fully unloaded (in the unloaded case, kickstart bootstraps it
    from the registered plist).

    Raises DaemonLifecycleError on non-zero launchctl exit so the
    caller (menubar notification surface) can show the message verbatim."""
    import os
    target = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    rc, out = _launchctl("kickstart", target)
    if rc != 0:
        # Common cause: service not yet bootstrapped (plist exists but
        # launchctl doesn't know about it).  Bootstrap then kickstart.
        bootstrap_rc, bootstrap_out = _launchctl(
            "bootstrap", f"gui/{os.getuid()}", str(plist_path()),
        )
        if bootstrap_rc != 0 and "service already loaded" not in bootstrap_out.lower():
            raise DaemonLifecycleError(
                f"Couldn't start daemon: {out or bootstrap_out or 'launchctl failed'}"
            )
        rc2, out2 = _launchctl("kickstart", target)
        if rc2 != 0:
            raise DaemonLifecycleError(
                f"Couldn't start daemon: {out2 or 'launchctl kickstart failed'}"
            )


def stop() -> None:
    """Stop the daemon (and unload it from launchd's user-domain database).

    Uses ``launchctl bootout`` rather than the deprecated ``unload`` —
    bootout is the launchd-supported way to fully stop and forget a
    service in modern macOS.  KeepAlive=true on the plist means a bare
    ``launchctl stop`` would just be respawned; bootout is the only
    reliable stop.

    Raises DaemonLifecycleError on non-zero launchctl exit unless the
    error is "service not loaded" (treat as already-stopped, success)."""
    import os
    target = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    rc, out = _launchctl("bootout", target)
    if rc != 0 and "could not find service" not in out.lower() \
            and "no such process" not in out.lower():
        raise DaemonLifecycleError(
            f"Couldn't stop daemon: {out or 'launchctl bootout failed'}"
        )


def restart() -> None:
    """Stop + start the daemon.

    Done as two separate launchctl calls (rather than ``launchctl
    kickstart -k`` which does it in one) because the bootout/bootstrap
    pair also picks up any plist changes since the last load — handy
    after a fulcra-collect upgrade.
    """
    try:
        stop()
    except DaemonLifecycleError as exc:
        # Continue to start() anyway — the user clicked Restart, they
        # want it running.  Surface the stop error in the start error
        # if start also fails.
        logger.warning("restart: stop step failed: %s", exc)
    start()

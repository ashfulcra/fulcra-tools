"""Best-effort launcher for the macOS menubar app.

When the daemon starts (whether via launchctl, `fulcra-collect daemon`,
or the future packaged .app), we want the menubar UI to come up
alongside it so the user sees a status icon without having to remember
a second command. This module owns that responsibility — and the
matching "is the menubar running?" probe the web UI uses to surface a
"Launch menubar app" button when the user has accidentally quit it.

Design notes:
  - macOS only. Linux/Windows users wouldn't get a menubar app anyway,
    so we early-return on non-darwin.
  - Best-effort: failures here NEVER block the daemon from starting.
    The menubar is a nice-to-have; the daemon is the source of truth.
  - We detect "already running" by process-name match (pgrep). The
    menubar process is named after its entry script (`fulcra-menubar`
    or `python -m fulcra_menubar`). Both patterns match.
  - We launch via `subprocess.Popen` and explicitly detach via
    `start_new_session=True` + closing stdio so the menubar survives
    the daemon's death. Mirrors how a `.app` bundle's parent-less
    process works.

The daemon calls `try_launch_menubar()` once at startup. The web UI
exposes `/api/menubar/status` and `/api/menubar/launch` so users can
relaunch from the dashboard if they've quit the menubar by accident.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal


_log = logging.getLogger("fulcra_collect.menubar")


MenubarStatus = Literal[
    "running",        # a fulcra-menubar process is alive on this user's session
    "not_running",    # menubar is installed/launchable but not currently running
    "not_installed",  # we couldn't find the menubar entry point on this machine
    "unsupported",    # non-macOS platform
]


def is_supported() -> bool:
    """Menubar app only exists on macOS."""
    return sys.platform == "darwin"


def is_running() -> bool:
    """True when a fulcra-menubar process is alive on this user's session.

    Uses `pgrep -fu <user>` so we don't catch other users' processes
    on a multi-user box. The patterns match both the `fulcra-menubar`
    script (uv-tool-installed) and the `python -m fulcra_menubar`
    invocation (uv-run from a checkout)."""
    if not is_supported():
        return False
    user = os.environ.get("USER") or _current_user()
    # Two patterns we want to catch:
    #   1. The script entry: /Users/.../bin/fulcra-menubar
    #   2. The module entry: python -m fulcra_menubar
    # `pgrep -f` matches the full command line; the regex covers both.
    pattern = r"fulcra[-_]menubar"
    try:
        result = subprocess.run(
            ["pgrep", "-fu", user, pattern],
            capture_output=True, text=True, timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # pgrep absent (shouldn't happen on macOS) or hung — treat as
        # "we don't know"; the caller falls back to "not_running" which
        # is the safer default.
        return False
    # pgrep returns 0 + pids on match, 1 + nothing on no match.
    if result.returncode != 0:
        return False
    # Filter out our own PID in case the daemon itself ever matches
    # (it shouldn't — its command line doesn't contain "menubar" —
    # but defence in depth).
    own_pid = str(os.getpid())
    pids = [p for p in result.stdout.strip().splitlines() if p and p != own_pid]
    return bool(pids)


def _current_user() -> str:
    """Fallback when $USER is unset (some launchd contexts)."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return ""


def find_menubar_command() -> list[str] | None:
    """Locate the fulcra-menubar entry point.

    Resolution order:
      1. `fulcra-menubar` on PATH (uv-tool-installed standalone)
      2. `python -m fulcra_menubar` from the repo checkout that's
         parent to this file (developer mode)
      3. None — we can't launch it.

    Returns the argv list to pass to subprocess.Popen, or None when
    the menubar isn't installed.
    """
    if not is_supported():
        return None
    direct = shutil.which("fulcra-menubar")
    if direct:
        return [direct]
    # Developer-mode fallback: this file lives at
    #   packages/collect/fulcra_collect/menubar_launcher.py
    # The menubar source is at packages/menubar/. We can spawn
    # `<python> -m fulcra_menubar` only if that package is importable
    # in our environment (which it is in `uv run --all-packages` /
    # `uv tool install fulcra-menubar` setups, not in standalone
    # `fulcra-collect` installs).
    try:
        import fulcra_menubar  # noqa: F401 — import test only
        return [sys.executable, "-m", "fulcra_menubar"]
    except ImportError:
        return None


def status() -> MenubarStatus:
    """Compose is_supported + is_running + find_menubar_command into
    the four-state enum the web UI surfaces."""
    if not is_supported():
        return "unsupported"
    if is_running():
        return "running"
    if find_menubar_command() is None:
        return "not_installed"
    return "not_running"


def try_launch_menubar(*, only_if_not_running: bool = True) -> bool:
    """Best-effort spawn of the menubar app.

    Returns True if we launched (or it was already running and
    only_if_not_running=True); False if we couldn't.

    Detaches the child from the daemon via `start_new_session=True`
    and closed stdio. The menubar survives daemon termination — same
    lifecycle a clicked .app bundle would have.

    NEVER raises. Daemon startup must not be blocked by menubar
    launch failures.
    """
    try:
        if not is_supported():
            return False
        if only_if_not_running and is_running():
            _log.debug("menubar already running; not launching")
            return True
        argv = find_menubar_command()
        if argv is None:
            _log.info(
                "menubar entry point not found on this machine — "
                "skipping auto-launch. Install with `uv tool install "
                "fulcra-menubar` or run from a checkout.",
            )
            return False
        # Detach: new session + closed stdio so the menubar outlives
        # the daemon process. devnull for stdin/out/err avoids the
        # rumps app blocking on terminal output buffers.
        with open(os.devnull, "rb") as devnull_in, \
             open(os.devnull, "wb") as devnull_out:
            subprocess.Popen(
                argv,
                stdin=devnull_in,
                stdout=devnull_out,
                stderr=devnull_out,
                start_new_session=True,
                close_fds=True,
            )
        _log.info("launched menubar app: %s", " ".join(argv))
        return True
    except Exception:
        # Wide except is intentional — see module docstring. Any
        # menubar launch failure is non-fatal to daemon startup.
        _log.exception("menubar auto-launch failed (non-fatal)")
        return False


def menubar_command_display() -> Path | str | None:
    """Surface the resolved command path for the web UI's status panel.

    Returns a Path-like string the user can recognise ('fulcra-menubar
    at /Users/…/bin/fulcra-menubar' or 'python -m fulcra_menubar from
    <interpreter>'), or None when find_menubar_command returns None."""
    argv = find_menubar_command()
    if argv is None:
        return None
    if len(argv) == 1:
        return argv[0]
    return " ".join(argv)

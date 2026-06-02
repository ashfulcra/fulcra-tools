"""Shared environment hardening for the launchd / cron installers (#25).

WHY THIS EXISTS: arc's live finding was that the heartbeat and listener jobs,
once installed, would not actually run. launchd starts a LaunchAgent with a
*bare* PATH (essentially ``/usr/bin:/bin:/usr/sbin:/sbin``) and cron runs with
an even more minimal one — neither includes Homebrew, ``~/.local/bin``, or the
``uv``-tool bin dir where ``fulcra-coord`` (or the ``uv`` that fronts it) lives.
So the resolved command failed to spawn and the job silently no-op'd, and the
operator had to hand-patch every plist before the schedule worked. On top of
that, launchd discards stdout/stderr by default, so when a job *did* fail there
was nowhere to look — debugging meant guessing.

This module centralizes the two fixes both installers need, so heartbeat.py and
listener.py stay byte-for-byte consistent (one PATH set, one log convention):

  * :func:`scheduler_path` — the PATH string baked into the plist's
    ``EnvironmentVariables`` and prefixed onto the cron line. It covers the
    common bins plus the directory of the *resolved* CLI argv[0], so whichever
    way the package was installed (Homebrew python, ``uv tool``, ``pip --user``,
    cargo-shimmed), the scheduled command can find its interpreter / entry point.
  * :func:`default_logs_dir` / log path naming — ``~/Library/Logs/fulcra-coord``
    so a failing job leaves a breadcrumb instead of vanishing.

stdlib-only. ``logs_dir`` is an overridable seam so tests never write under the
real ``~/Library``; ``home`` is injectable for the same reason.
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_macos() -> bool:
    """True on macOS, where the installers target launchd; elsewhere they use cron.

    Lives here (not in each installer) so heartbeat.py and listener.py agree on the
    platform branch from a single source — they previously carried byte-identical
    private copies of this check.
    """
    return sys.platform == "darwin"


def launchagents_dir() -> Path:
    """``~/Library/LaunchAgents`` — where a user-session LaunchAgent plist is dropped.

    Shared by both installers (was duplicated verbatim in each) so the launchd
    install location is defined once.
    """
    return Path.home() / "Library" / "LaunchAgents"

# The common interactive-shell bins a user-session job is expected to find but
# which launchd/cron do NOT put on PATH by default. Ordered most-specific-first
# (Homebrew, then the user's local installs, then the system bins) so a
# user-installed binary shadows a system one, matching interactive-shell intent.
_COMMON_BINS_TEMPLATE = [
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "{home}/.local/bin",
    "{home}/.cargo/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
]


def scheduler_path(cli_argv: list[str], *, home: "str | Path | None" = None) -> str:
    """Build the PATH string for a scheduled (launchd/cron) fulcra-coord job.

    Covers the common interactive bins (Homebrew, ~/.local/bin, ~/.cargo/bin,
    system) PLUS the directory of the resolved CLI's argv[0] — so ``uv`` /
    ``fulcra-coord`` resolve regardless of how the package was installed, which
    is the whole point of #25 (the bare launchd/cron PATH could not find them).

    The CLI dir is prepended (highest precedence) because it is the most
    specific, install-time-known location of the exact binary we need to run.
    De-duplicated while preserving order so a CLI dir that is already one of the
    common bins does not appear twice.
    """
    h = Path(home) if home is not None else Path.home()
    entries: list[str] = []

    # The resolved CLI's own directory first — argv[0] is an absolute path when
    # resolve_cli_argv found an on-PATH entry point, or sys.executable for the
    # `python -m` fallback; either way its parent dir is where the runnable
    # lives. A bare/relative argv[0] (no dir component) contributes nothing.
    if cli_argv:
        cli_dir = Path(cli_argv[0]).parent
        if str(cli_dir) not in ("", "."):
            entries.append(str(cli_dir))

    for tmpl in _COMMON_BINS_TEMPLATE:
        entries.append(tmpl.format(home=h))

    seen: set[str] = set()
    deduped = [e for e in entries if not (e in seen or seen.add(e))]
    return ":".join(deduped)


def default_logs_dir(*, home: "str | Path | None" = None) -> Path:
    """``~/Library/Logs/fulcra-coord`` — where scheduled jobs write stdout/stderr.

    Overridable via ``home`` (and, at the installer layer, via an explicit
    ``logs_dir`` arg) so tests never touch the real ~/Library.
    """
    h = Path(home) if home is not None else Path.home()
    return h / "Library" / "Logs" / "fulcra-coord"


def log_paths(logs_dir: Path, stem: str) -> tuple[str, str]:
    """(StandardOutPath, StandardErrorPath) under ``logs_dir`` for job ``stem``
    (``heartbeat`` / ``listener``): ``<stem>.out.log`` / ``<stem>.err.log``."""
    return (str(logs_dir / f"{stem}.out.log"),
            str(logs_dir / f"{stem}.err.log"))

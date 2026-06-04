"""Scheduled operator-digest installer (calendar-based, twice daily).

WHY a NEW module (vs reusing heartbeat.py): the heartbeat is INTERVAL-scheduled
(StartInterval / */N cron) — "every 20 min". The digest is CALENDAR-scheduled —
"at 08:00 and at 18:00, local". launchd expresses that with StartCalendarInterval
(not StartInterval), and cron with fixed ``M H * * *`` lines (not ``*/N``), so the
plist/cron shapes genuinely differ. We DO reuse scheduler_env (the #25 PATH +
log-paths hardening) and cli_invocation (resolve_cli_argv) verbatim, so the
scheduled command resolves under uv-tool / source installs exactly like the
heartbeat/listener jobs.

Two windows -> two jobs: ``digest --window morning`` at 08:00 and
``digest --window evening`` at 18:00. Installable on any/all machines; the
any-agent dedup marker (cli._claim_digest_marker) makes concurrent installs safe
(first writer wins, others no-op). Contract mirrors the other installers:
idempotent, dry-run writes nothing, surgical uninstall, fail-safe, stdlib-only.
``target_dir`` / ``crontab_path`` / ``logs_dir`` are overridable so tests never
touch the real scheduler or ~/Library.
"""
from __future__ import annotations

import plistlib
import shlex
from pathlib import Path
from typing import Any

from . import cli_invocation
from . import scheduler_env

LABEL_PREFIX = "com.fulcra.coord.digest"
LOG_STEM = "digest"
CRON_MARKER = "# fulcra-coord-digest (managed; do not edit this line)"

#: The two cadence windows and their wall-clock time-of-day (local). 08:00 / 18:00
#: per the spec; the dedup marker is keyed by UTC date so a slightly different
#: local fire time across machines still collapses to one digest per window.
WINDOWS = (("morning", 8, 0), ("evening", 18, 0))


def _label_for(window: str) -> str:
    return f"{LABEL_PREFIX}.{window}"


def _plist_name_for(window: str) -> str:
    return f"{_label_for(window)}.plist"


def _digest_args(window: str) -> list[str]:
    """The subcommand tail: write this window's digest. One call per job."""
    return ["digest", "--window", window]


def _plist_body(argv: list[str], window: str, hour: int, minute: int,
                logs_dir: Path) -> str:
    """A launchd plist running ``<argv...> digest --window <window>`` at a fixed
    time of day via StartCalendarInterval (NOT StartInterval — this is a
    calendar job, not an interval one). Built via plistlib so a spaced argv[0]
    survives and values are XML-escaped. #25 hardening: bakes the PATH + log
    paths so launchd's bare env can find the binary and a failure leaves a log."""
    out_path, err_path = scheduler_env.log_paths(logs_dir, f"{LOG_STEM}.{window}")
    body: dict[str, Any] = {
        "Label": _label_for(window),
        "ProgramArguments": list(argv) + _digest_args(window),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": {"PATH": scheduler_env.scheduler_path(argv)},
        "StandardOutPath": out_path,
        "StandardErrorPath": err_path,
    }
    return plistlib.dumps(body).decode("utf-8")


def _cron_line(argv: list[str], window: str, hour: int, minute: int) -> str:
    """A crontab entry running the window's digest at ``minute hour * * *``,
    tagged with the managed marker so uninstall is surgical. argv is shell-quoted
    token-by-token (a spaced path stays one word); #25 PATH prefix so cron's
    minimal PATH can find the binary."""
    schedule = f"{minute} {hour} * * *"
    path = shlex.quote(scheduler_env.scheduler_path(argv))
    cmd = " ".join(shlex.quote(t) for t in (list(argv) + _digest_args(window)))
    return f"{CRON_MARKER}\n{schedule} PATH={path} {cmd} >/dev/null 2>&1\n"


def _is_managed_cron_command(line: str) -> bool:
    """True when a crontab line (after the marker) is one of OUR managed digest
    entries — a ``M H * * * <argv> digest --window <w>`` command with our
    redirection suffix. Lets uninstall drop only genuinely-ours lines and
    preserve an unrelated user job that happens to follow a stray marker."""
    s = line.rstrip("\n")
    return (" digest " in f" {s} "
            and " --window " in f" {s} "
            and s.rstrip().endswith(">/dev/null 2>&1"))


def _strip_managed_cron(text: str) -> str:
    """Remove every managed marker line and the managed digest command that
    follows it (M2: only when that next line is genuinely ours). Every
    user-owned line is preserved — surgical uninstall."""
    out: list[str] = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if lines[i].rstrip("\n") == CRON_MARKER:
            if i + 1 < len(lines) and _is_managed_cron_command(lines[i + 1]):
                i += 2
            else:
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def install_digest(*, uninstall: bool = False, dry_run: bool = False,
                   target_dir: "str | Path | None" = None,
                   crontab_path: "str | Path | None" = None,
                   logs_dir: "str | Path | None" = None,
                   force_cron: bool = False) -> dict[str, Any]:
    """Install/uninstall the twice-daily scheduled ``fulcra-coord digest`` jobs.

    macOS -> two launchd plists (morning 08:00 / evening 18:00) in ``target_dir``
    (default ~/Library/LaunchAgents); other platforms (or ``force_cron=True``) ->
    two managed crontab lines in ``crontab_path``. Idempotent, surgical uninstall,
    dry-run writes nothing. ``force_cron`` is a test seam to exercise the cron
    branch on a macOS dev box. The any-agent dedup guard makes installing this on
    every machine safe (concurrent ticks collapse to one digest per window)."""
    argv = cli_invocation.resolve_cli_argv()
    use_cron = force_cron or not scheduler_env.is_macos()
    plan: dict[str, Any] = {
        "mechanism": "crontab" if use_cron else "launchd",
        "cli_command": cli_invocation.resolve_cli_command(),
        "windows": [w for w, _, _ in WINDOWS],
        "uninstall": uninstall,
        "dry_run": dry_run,
        "writes": [],
        "removes": [],
    }

    if not use_cron:
        base = Path(target_dir) if target_dir is not None else scheduler_env.launchagents_dir()
        logs = Path(logs_dir) if logs_dir is not None else scheduler_env.default_logs_dir()
        for window, hour, minute in WINDOWS:
            plist = base / _plist_name_for(window)
            if uninstall:
                plan["removes"].append(str(plist))
                if not dry_run and plist.exists():
                    plist.unlink()
                continue
            plan["writes"].append(str(plist))
            if not dry_run:
                base.mkdir(parents=True, exist_ok=True)
                logs.mkdir(parents=True, exist_ok=True)
                plist.write_text(_plist_body(argv, window, hour, minute, logs))
        return plan

    # --- crontab branch ----------------------------------------------------
    cron = Path(crontab_path) if crontab_path is not None else None
    if cron is None:
        base = Path(target_dir) if target_dir is not None else Path.home()
        cron = base / "fulcra-coord-digest-crontab.txt"
    existing = cron.read_text() if cron.is_file() else ""
    stripped = _strip_managed_cron(existing)
    if uninstall:
        if stripped != existing:
            plan["removes"].append(str(cron))
        if not dry_run:
            cron.parent.mkdir(parents=True, exist_ok=True)
            cron.write_text(stripped)
        return plan

    new_text = (stripped.rstrip("\n") + "\n" if stripped.strip() else "")
    for window, hour, minute in WINDOWS:
        new_text += _cron_line(argv, window, hour, minute)
    plan["writes"].append(str(cron))
    if not dry_run:
        cron.parent.mkdir(parents=True, exist_ok=True)
        cron.write_text(new_text)
    return plan

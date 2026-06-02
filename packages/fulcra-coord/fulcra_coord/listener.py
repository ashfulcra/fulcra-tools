"""Durable per-agent inbox listener (Part 3): notice directed work while idle.

WHY this exists: the SessionStart hook surfaces directives the instant a session
opens (pull), and the heartbeat keeps the reconciler sweeping stale work — but
neither *notices a new directive arriving while the agent is idle between
sessions*. The only "listening" before this was an ad-hoc ``run_in_background``
poller: session-scoped, capped at the session lifetime, and gone the moment the
session ended. We need a durable, periodic, per-agent listener that survives
across sessions in every environment.

Notify-only by design (locked in the spec): the listener NEVER runs the
directive. It polls the inbox, writes a surface file the next SessionStart can
inject, and emits a best-effort notification. A human / the next session decides
what to do. (A future ``--auto`` policy mode is explicitly out of scope.)

Three scheduling mechanisms, one per environment:

  * **Claude Code -> a scheduled remote agent.** A recurring headless Claude run
    (the harness scheduler / ``/schedule`` routine) whose job is one call:
    ``fulcra-coord notify-inbox --agent <me>``. Survives across sessions with no
    app window. That routine is created via the harness scheduler, not this
    module; see ``adapters/claude-code/LISTENER.md``. This module provides the
    launchd/cron fallback that works without the harness.
  * **OpenClaw -> heartbeat.** The shipped ``HEARTBEAT.md`` runs ``notify-inbox``
    each beat (see fulcra_coord.openclaw).
  * **Generic -> launchd/crontab.** ``install_listener`` materializes a launchd
    LaunchAgent on macOS (default) or a managed crontab line elsewhere, exactly
    mirroring ``install_heartbeat``'s installer contract.

The scheduled command is resolved through ``resolve_cli_argv()`` (Gap 1) so it
works under uv-tool / source installs, materialized as real plist ``<array>``
elements / ``shlex``-quoted cron tokens so a spaced interpreter path survives.

Contract (mirrors the other installers): idempotent, dry-run writes nothing but
reports the plan, surgical uninstall, fail-safe. stdlib-only. ``target_dir`` /
``crontab_path`` are overridable so tests never touch the real
~/Library/LaunchAgents or the live crontab.
"""
from __future__ import annotations

import plistlib
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import cli_invocation
from . import scheduler_env
from .views import agent_slug  # one source of truth for the agent->slug mapping

# launchd label / plist basename. Also used as the crontab managed marker so
# uninstall can find exactly our line. Distinct from the heartbeat's label so a
# machine can run BOTH the heartbeat (reconcile) and the listener (notify-inbox)
# without their managed entries colliding.
LABEL = "com.fulcra.coord.listener"
PLIST_NAME = f"{LABEL}.plist"
CRON_MARKER = "# fulcra-coord-listener (managed; do not edit this line)"

# stdout/stderr basename stem under ~/Library/Logs/fulcra-coord (#25): a failing
# listener job leaves listener.{out,err}.log instead of vanishing.
LOG_STEM = "listener"

INTERVAL_MIN_DEFAULT = 10


def _notify_args(agent: str) -> list[str]:
    """The subcommand tail appended to the resolved CLI argv: poll this agent's
    inbox, surface + notify. One call so the scheduler line is a single command."""
    return ["notify-inbox", "--agent", agent]


def _plist_body(argv: list[str], agent: str, interval_sec: int,
                logs_dir: Path) -> str:
    """A launchd plist that runs ``<argv...> notify-inbox --agent <me>`` every
    interval_sec. Built via plistlib from an explicit argv (C1 + M1): each token
    is a real ProgramArguments element (no word-splitting on a spaced argv[0]),
    and plistlib XML-escapes every value so a path with ``&``/``<``/``>`` can't
    break the document.

    #25 hardening: bakes ``EnvironmentVariables.PATH`` (common bins + the
    resolved CLI's own dir) since launchd's bare PATH cannot find the binary, and
    ``StandardOut/ErrorPath`` so a failing tick leaves a log."""
    out_path, err_path = scheduler_env.log_paths(logs_dir, LOG_STEM)
    body: dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": list(argv) + _notify_args(agent),
        "StartInterval": interval_sec,
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": scheduler_env.scheduler_path(argv)},
        "StandardOutPath": out_path,
        "StandardErrorPath": err_path,
    }
    return plistlib.dumps(body).decode("utf-8")


def _cron_line(argv: list[str], agent: str, interval_min: int) -> str:
    """A crontab entry running notify-inbox every interval_min minutes, tagged
    with the managed marker so uninstall is surgical. argv + the agent arg are
    shell-quoted token-by-token (C1) so a spaced path / agent id stays one word
    when cron runs the line through /bin/sh.

    #25 hardening: prefixed with ``PATH=<common bins + CLI dir>`` (shell-quoted as
    one word) so cron's minimal PATH can find the binary. The PATH= lives on the
    command line so the managed entry stays a single marker-tagged line and
    surgical uninstall still matches exactly our command."""
    schedule = f"*/{interval_min} * * * *"
    path = shlex.quote(scheduler_env.scheduler_path(argv))
    cmd = " ".join(shlex.quote(t) for t in (list(argv) + _notify_args(agent)))
    return f"{CRON_MARKER}\n{schedule} PATH={path} {cmd} >/dev/null 2>&1\n"


def _is_managed_cron_command(line: str) -> bool:
    """True when a crontab line (after the marker) is OUR managed notify-inbox
    entry. Like the heartbeat's guard (M2): if the marker is instead followed by
    an unrelated user job, preserve that line and drop only the orphaned marker.
    Match on the cron-schedule shape + the notify-inbox subcommand + our exact
    redirection suffix, which together are specific to the line we generate."""
    s = line.rstrip("\n")
    return (
        s.startswith("*/")
        and " notify-inbox " in f" {s} "
        and s.rstrip().endswith(">/dev/null 2>&1")
    )


def _strip_managed_cron(text: str) -> str:
    """Remove the managed marker line, and the line that follows it ONLY when
    that line is our managed notify-inbox command (M2). Every other (user-owned)
    crontab line is left untouched."""
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


def install_listener(*, agent: str, interval_min: int = INTERVAL_MIN_DEFAULT,
                     uninstall: bool = False, dry_run: bool = False,
                     target_dir: "str | Path | None" = None,
                     crontab_path: "str | Path | None" = None,
                     logs_dir: "str | Path | None" = None) -> dict[str, Any]:
    """Install/uninstall a scheduled ``fulcra-coord notify-inbox --agent`` job.

    macOS -> launchd plist in ``target_dir`` (default ~/Library/LaunchAgents);
    other platforms -> a managed crontab line in ``crontab_path``. ``target_dir``
    / ``crontab_path`` / ``logs_dir`` are overridable so tests never touch the
    real scheduler or ~/Library. Idempotent, surgical uninstall, dry-run writes
    nothing. Cadence is per-agent-configurable (default 10 min). On macOS the
    plist carries a hardened PATH + log paths (#25); the ``logs_dir`` (default
    ~/Library/Logs/fulcra-coord) is created on install.
    """
    argv = cli_invocation.resolve_cli_argv()
    interval_sec = interval_min * 60
    plan: dict[str, Any] = {
        "mechanism": "launchd" if scheduler_env.is_macos() else "crontab",
        "cli_command": cli_invocation.resolve_cli_command(),
        "agent": agent,
        "interval_min": interval_min,
        "uninstall": uninstall,
        "dry_run": dry_run,
        "writes": [],
        "removes": [],
    }

    if scheduler_env.is_macos():
        base = Path(target_dir) if target_dir is not None else scheduler_env.launchagents_dir()
        logs = Path(logs_dir) if logs_dir is not None \
            else scheduler_env.default_logs_dir()
        plist = base / PLIST_NAME
        if uninstall:
            plan["removes"].append(str(plist))
            if not dry_run and plist.exists():
                plist.unlink()
            return plan
        plan["writes"].append(str(plist))
        if not dry_run:
            base.mkdir(parents=True, exist_ok=True)
            # Create the Logs dir so launchd's StandardOut/ErrorPath is writable
            # from the first tick (launchd will not create it for us).
            logs.mkdir(parents=True, exist_ok=True)
            plist.write_text(_plist_body(argv, agent, interval_sec, logs))
        return plan

    # --- crontab fallback (Linux / other) ----------------------------------
    cron = Path(crontab_path) if crontab_path is not None else None
    if cron is None:
        base = Path(target_dir) if target_dir is not None else Path.home()
        cron = base / "fulcra-coord-listener-crontab.txt"
    existing = cron.read_text() if cron.is_file() else ""
    stripped = _strip_managed_cron(existing)
    if uninstall:
        if stripped != existing:
            plan["removes"].append(str(cron))
        if not dry_run:
            cron.parent.mkdir(parents=True, exist_ok=True)
            cron.write_text(stripped)
        return plan

    new_text = (stripped.rstrip("\n") + "\n" if stripped.strip() else "") \
        + _cron_line(argv, agent, interval_min)
    plan["writes"].append(str(cron))
    if not dry_run:
        cron.parent.mkdir(parents=True, exist_ok=True)
        cron.write_text(new_text)
    return plan


def emit_notification(agent: str, count: int) -> None:
    """Best-effort desktop notification that `count` directives await `agent`.

    macOS -> osascript ``display notification`` (the user-session notifier where
    the operator's agents run); elsewhere -> a line on stderr (which cron/launchd
    capture to their logs). Swallows EVERY error: a failed notification must
    never break the scheduled job (fail-safe contract). Pure side-effect, no
    return — callers decide whether to call it (skip on empty inbox)."""
    msg = f"{count} directive(s) waiting in your fulcra-coord inbox"
    try:
        if scheduler_env.is_macos():
            subprocess.run(
                ["osascript", "-e",
                 f'display notification {_osa_quote(msg)} '
                 f'with title "fulcra-coord"'],
                capture_output=True, timeout=5, check=False,
            )
        else:
            print(f"[fulcra-coord] {agent}: {msg}", file=sys.stderr)
    except Exception:
        # Notification is advisory; never let it fail the listener tick.
        pass


def _osa_quote(s: str) -> str:
    """Quote a string for an AppleScript literal (double-quote delimited):
    backslash-escape backslashes and double-quotes so a directive count string
    can't break out of the osascript expression."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

"""Scheduled heartbeat installer (Gap 2): keep the reconciler running.

WHY this exists: the reconciler is the coordination suite's safety net. Crashed
agents (whose SessionEnd hook never fired) and ChatGPT (which has no end hook at
all) leave `active` tasks dangling; nothing sweeps them back to a sane state and
no one recomputes staleness. The per-session hooks cannot cover a process that
died or an agent with no lifecycle events. A scheduled `fulcra-coord reconcile`
is the backstop — it re-materializes the views (stamping the stale flag and
building needs-attention.json) on a fixed cadence regardless of agent liveness.

Two scheduling mechanisms, picked by platform:

  * macOS -> a launchd LaunchAgent plist under ~/Library/LaunchAgents. This is
    the user-session scheduler on macOS (where the operator's agents run); a
    `StartInterval` re-runs the command every N seconds. launchd survives logout
    less than a daemon would, but matches the user's interactive env, which is
    where Fulcra auth/PATH live.
  * everything else -> a crontab line tagged with a managed marker comment so we
    can surgically add/remove exactly our entry without disturbing the user's
    other cron jobs.

The scheduled command is resolved through resolve_cli_argv() (Gap 1) so the
heartbeat works under uv-tool / source installs, not just pip-on-PATH. The argv
is materialized as real plist <array> elements / shlex-quoted cron tokens (C1),
so a resolved interpreter path containing a space survives intact.

Contract (mirrors the other installers): idempotent, dry-run writes nothing but
reports the plan, surgical uninstall, fail-safe. stdlib-only (the plist/cron are
plain text). `target_dir` / `crontab_path` are overridable so tests never touch
the real ~/Library/LaunchAgents or the live crontab.
"""
from __future__ import annotations

import plistlib
import shlex
from pathlib import Path
from typing import Any

from . import cli_invocation
from . import scheduler_env

# launchd label / plist basename. Also used as the crontab managed marker so
# uninstall can find exactly our line.
LABEL = "com.fulcra.coord.heartbeat"

# stdout/stderr basename stem under ~/Library/Logs/fulcra-coord (#25): a failing
# heartbeat job leaves heartbeat.{out,err}.log instead of vanishing.
LOG_STEM = "heartbeat"
PLIST_NAME = f"{LABEL}.plist"
CRON_MARKER = "# fulcra-coord-heartbeat (managed; do not edit this line)"

INTERVAL_MIN_DEFAULT = 20


def _plist_body(argv: list[str], interval_sec: int, logs_dir: Path) -> str:
    """A launchd plist that runs `<argv...> reconcile` every interval_sec.

    Built from an explicit argv via ``plistlib.dumps`` (C1 + M1): each token is a
    real ``ProgramArguments`` element with no word-splitting, so a token
    containing a space survives intact; and plistlib XML-escapes every value, so
    a path with ``&``/``<``/``>`` cannot break the document (M1). launchd does
    not word-split ProgramArguments, so a spaced argv[0] is preserved.

    #25 hardening: launchd starts the job with a bare PATH, so we bake an
    ``EnvironmentVariables.PATH`` (common bins + the resolved CLI's own dir) or
    the command cannot be found; and ``StandardOut/ErrorPath`` so a failure
    leaves a log instead of vanishing.
    """
    out_path, err_path = scheduler_env.log_paths(logs_dir, LOG_STEM)
    body: dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": list(argv) + ["reconcile"],
        "StartInterval": interval_sec,
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": scheduler_env.scheduler_path(argv)},
        "StandardOutPath": out_path,
        "StandardErrorPath": err_path,
    }
    return plistlib.dumps(body).decode("utf-8")


def _cron_line(argv: list[str], interval_min: int) -> str:
    """A crontab entry running `<argv...> reconcile` every interval_min minutes,
    tagged with the managed marker so uninstall is surgical.

    The argv is shell-quoted token-by-token (C1) so a spaced argv[0] stays a
    single shell word; cron runs the line through ``/bin/sh``, which would
    otherwise word-split an unquoted path with a space.

    #25 hardening: the line is prefixed with ``PATH=<common bins + CLI dir>`` so
    cron's minimal PATH can find the binary; ``shlex.quote`` keeps the PATH a
    single word. The PATH= assignment lives on the command line (not a separate
    crontab env line) so the whole managed entry stays one marker-tagged line and
    surgical uninstall still removes exactly our command.
    """
    schedule = f"*/{interval_min} * * * *"
    path = shlex.quote(scheduler_env.scheduler_path(argv))
    cmd = " ".join(shlex.quote(t) for t in argv) + " reconcile"
    return f"{CRON_MARKER}\n{schedule} PATH={path} {cmd} >/dev/null 2>&1\n"


def _is_managed_cron_command(line: str) -> bool:
    """True when a crontab line (after the marker) is OUR managed reconcile entry.

    M2: we only drop the line *following* the marker when it is genuinely ours —
    a ``*/N * * * * <argv> reconcile`` command. If the marker is instead followed
    by an unrelated user job (e.g. the managed line was hand-removed but the
    marker stayed, or lines were reordered), we must preserve that user line and
    drop only the orphaned marker. Match on the cron-schedule shape + the
    ``reconcile`` subcommand + the redirection suffix our writer emits, which
    together are specific to the line we generate.
    """
    s = line.rstrip("\n")
    return (
        s.startswith("*/")
        and " reconcile " in f" {s} "
        and s.rstrip().endswith(">/dev/null 2>&1")
    )


def _strip_managed_cron(text: str) -> str:
    """Remove the managed marker line, and the line that follows it ONLY when
    that line is our managed reconcile command (M2). Every other (user-owned)
    crontab line — including an unrelated job that happens to sit right after a
    stray marker — is left untouched."""
    out: list[str] = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if lines[i].rstrip("\n") == CRON_MARKER:
            # Always drop the marker. Drop the NEXT line too only if it is ours.
            if i + 1 < len(lines) and _is_managed_cron_command(lines[i + 1]):
                i += 2
            else:
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def install_heartbeat(*, interval_min: int = INTERVAL_MIN_DEFAULT,
                      uninstall: bool = False, dry_run: bool = False,
                      target_dir: "str | Path | None" = None,
                      crontab_path: "str | Path | None" = None,
                      logs_dir: "str | Path | None" = None) -> dict[str, Any]:
    """Install/uninstall a scheduled `fulcra-coord reconcile` heartbeat.

    macOS -> launchd plist in ``target_dir`` (default ~/Library/LaunchAgents);
    other platforms -> a managed crontab line in ``crontab_path``. ``target_dir``
    / ``crontab_path`` / ``logs_dir`` are overridable so tests never touch the
    real scheduler or ~/Library. Idempotent, surgical uninstall, dry-run writes
    nothing. On macOS the plist carries a hardened PATH + log paths (#25); the
    ``logs_dir`` (default ~/Library/Logs/fulcra-coord) is created on install.
    """
    argv = cli_invocation.resolve_cli_argv()
    interval_sec = interval_min * 60
    plan: dict[str, Any] = {
        "mechanism": "launchd" if scheduler_env.is_macos() else "crontab",
        # Display string only (shell-quoted); the scheduler entries use argv.
        "cli_command": cli_invocation.resolve_cli_command(),
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
            # Create the Logs dir so launchd's StandardOut/ErrorPath can be
            # written from the first tick (launchd will not mkdir it for us).
            logs.mkdir(parents=True, exist_ok=True)
            plist.write_text(_plist_body(argv, interval_sec, logs))
        return plan

    # --- crontab fallback (Linux / other) ----------------------------------
    cron = Path(crontab_path) if crontab_path is not None else None
    if cron is None:
        # No explicit path: operate on a managed file under target_dir (or HOME)
        # rather than mutating the live crontab, keeping the installer testable
        # and the CLI layer responsible for any `crontab` reload.
        base = Path(target_dir) if target_dir is not None else Path.home()
        cron = base / "fulcra-coord-crontab.txt"
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
        + _cron_line(argv, interval_min)
    plan["writes"].append(str(cron))
    if not dry_run:
        cron.parent.mkdir(parents=True, exist_ok=True)
        cron.write_text(new_text)
    return plan

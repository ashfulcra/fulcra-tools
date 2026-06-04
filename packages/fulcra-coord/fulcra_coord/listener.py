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
    LaunchAgent on macOS (default) or a managed crontab line elsewhere, mirroring
    ``install_heartbeat``'s installer contract — except the listener is PER-AGENT,
    not per-machine. Because each job polls one agent's inbox
    (``notify-inbox --agent X``), the launchd label / plist basename / cron marker
    are all derived from ``agent_slug(agent)`` (the same slug as the inbox view
    files — one source of truth). Co-located agents (e.g. ``codex:Mac:main`` and
    ``claude-code:Mac:fulcra-tools``) therefore get DISTINCT coexisting jobs; a
    second agent's install no longer clobbers the first's. (The heartbeat stays a
    correct singleton — one reconcile sweep per machine — and is untouched.) A
    pre-0.5.3 machine-global job is migrated on install: superseded if it watched
    THIS agent, left alone if it watched another.

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

# launchd label / plist basename PREFIX. The listener is inherently PER-AGENT
# (it polls ``notify-inbox --agent X``), so every co-located agent needs its own
# launchd job / cron block — a single machine-global label made install B
# clobber install A, leaving only one inbox watched (the bug this module fixes).
# Identity is derived from ``agent_slug`` (the SAME slug used for inbox view
# files — one source of truth) via the ``_*_for(agent)`` helpers below.
#
# ``LABEL_PREFIX`` is also distinct from the heartbeat's label so a machine can
# run BOTH the heartbeat (a correct singleton: one reconcile sweep per machine)
# and any number of per-agent listeners without their managed entries colliding.
LABEL_PREFIX = "com.fulcra.coord.listener"

# The OLD machine-global identity (no slug). A listener installed by a pre-0.5.3
# build wrote this single plist / used this bare marker. On install we migrate
# it: if the legacy job watches THIS agent it is superseded by the agent's new
# per-agent job (else it's left for that agent to migrate on its own reinstall).
LEGACY_LABEL = LABEL_PREFIX
LEGACY_PLIST_NAME = f"{LEGACY_LABEL}.plist"
LEGACY_CRON_MARKER = "# fulcra-coord-listener (managed; do not edit this line)"


def _label_for(agent: str) -> str:
    """Per-agent launchd label: ``com.fulcra.coord.listener.<slug>``. Derived
    from ``agent_slug`` so the launchd identity, the cron marker, and the inbox
    view file all key off the SAME slug — one source of truth, no drift."""
    return f"{LABEL_PREFIX}.{agent_slug(agent)}"


def _plist_name_for(agent: str) -> str:
    """Per-agent plist basename: ``<label>.plist``. Two different agents map to
    two different basenames, so their plists COEXIST in ~/Library/LaunchAgents
    instead of one overwriting the other."""
    return f"{_label_for(agent)}.plist"


def _cron_marker_for(agent: str) -> str:
    """Per-agent managed crontab marker embedding the slug, so surgical uninstall
    finds exactly THIS agent's block and leaves every other agent's intact."""
    return (f"# fulcra-coord-listener:{agent_slug(agent)} "
            "(managed; do not edit this line)")

# stdout/stderr basename stem under ~/Library/Logs/fulcra-coord (#25): a failing
# listener job leaves listener.{out,err}.log instead of vanishing.
LOG_STEM = "listener"

INTERVAL_MIN_DEFAULT = 10


def _notify_args(agent: str) -> list[str]:
    """The subcommand tail appended to the resolved CLI argv: poll this agent's
    inbox, surface + notify. One call so the scheduler line is a single command."""
    return ["notify-inbox", "--agent", agent]


def _plist_body(argv: list[str], agent: str, interval_sec: int,
                logs_dir: Path, label: str) -> str:
    """A launchd plist that runs ``<argv...> notify-inbox --agent <me>`` every
    interval_sec. Built via plistlib from an explicit argv (C1 + M1): each token
    is a real ProgramArguments element (no word-splitting on a spaced argv[0]),
    and plistlib XML-escapes every value so a path with ``&``/``<``/``>`` can't
    break the document.

    ``label`` is the PER-AGENT label (``_label_for(agent)``) threaded through by
    the caller — distinct per agent so co-located listeners don't share a launchd
    identity.

    #25 hardening: bakes ``EnvironmentVariables.PATH`` (common bins + the
    resolved CLI's own dir) since launchd's bare PATH cannot find the binary, and
    ``StandardOut/ErrorPath`` so a failing tick leaves a log."""
    out_path, err_path = scheduler_env.log_paths(logs_dir, LOG_STEM)
    body: dict[str, Any] = {
        "Label": label,
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
    with THIS agent's per-agent managed marker so uninstall is surgical and only
    touches one agent's block. argv + the agent arg are shell-quoted
    token-by-token (C1) so a spaced path / agent id stays one word when cron runs
    the line through /bin/sh.

    #25 hardening: prefixed with ``PATH=<common bins + CLI dir>`` (shell-quoted as
    one word) so cron's minimal PATH can find the binary. The PATH= lives on the
    command line so the managed entry stays a single marker-tagged line and
    surgical uninstall still matches exactly our command."""
    schedule = f"*/{interval_min} * * * *"
    path = shlex.quote(scheduler_env.scheduler_path(argv))
    cmd = " ".join(shlex.quote(t) for t in (list(argv) + _notify_args(agent)))
    return (f"{_cron_marker_for(agent)}\n"
            f"{schedule} PATH={path} {cmd} >/dev/null 2>&1\n")


def _is_managed_cron_command(line: str, agent: str) -> bool:
    """True when a crontab line (after a marker) is the managed notify-inbox
    entry FOR THIS agent. Like the heartbeat's guard (M2): if a marker is instead
    followed by an unrelated user job, preserve that line and drop only the
    orphaned marker.

    Match on the cron-schedule shape + ``--agent <agent>`` (so one agent's strip
    never claims another's line) + our exact redirection suffix, which together
    are specific to the line we generate for ``agent``."""
    s = line.rstrip("\n")
    return (
        s.startswith("*/")
        and f" --agent {shlex.quote(agent)} " in f" {s} "
        and s.rstrip().endswith(">/dev/null 2>&1")
    )


def _strip_managed_cron(text: str, agent: str) -> str:
    """Remove THIS agent's per-agent managed marker line (and the legacy
    un-slugged marker when it precedes this agent's command), plus the command
    line that follows ONLY when it's this agent's managed notify-inbox command
    (M2). Other agents' managed blocks and every user-owned line are left
    untouched — surgical, agent-scoped uninstall."""
    marker = _cron_marker_for(agent)
    out: list[str] = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        stripped = lines[i].rstrip("\n")
        # This agent's per-agent marker, OR a legacy un-slugged marker that is
        # followed by THIS agent's command (legacy supersede — leave a legacy
        # marker that fronts a different agent's command alone).
        is_my_marker = stripped == marker
        is_legacy_for_me = (
            stripped == LEGACY_CRON_MARKER
            and i + 1 < len(lines)
            and _is_managed_cron_command(lines[i + 1], agent)
        )
        if is_my_marker or is_legacy_for_me:
            if i + 1 < len(lines) and _is_managed_cron_command(lines[i + 1], agent):
                i += 2
            else:
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def _launchctl_unload(plist: Path) -> None:
    """Best-effort ``launchctl unload`` of a plist (used when superseding the
    legacy job). Swallows every error and never raises: launchctl may be absent
    (non-interactive context, CI), the job may already be unloaded, or we may not
    own the GUI session. Removing the plist file is the durable change; the
    unload just stops a currently-running legacy tick sooner."""
    try:
        subprocess.run(["launchctl", "unload", "-w", str(plist)],
                       capture_output=True, timeout=5, check=False)
    except Exception:
        pass


def _legacy_plist_agent(plist: Path) -> "str | None":
    """The ``--agent`` value a legacy plist's ProgramArguments watches, or None
    if the file is unreadable / has no ``--agent``. Used to decide whether a
    legacy un-slugged plist is superseded by THIS agent's new per-agent plist
    (same agent -> supersede) or left for its own agent to migrate (different
    agent). Best-effort: any parse error yields None (treated as "not mine")."""
    try:
        with plist.open("rb") as f:
            body = plistlib.load(f)
        args = body.get("ProgramArguments") or []
        for i, tok in enumerate(args):
            if tok == "--agent" and i + 1 < len(args):
                return args[i + 1]
    except Exception:
        pass
    return None


def install_listener(*, agent: str, interval_min: int = INTERVAL_MIN_DEFAULT,
                     uninstall: bool = False, dry_run: bool = False,
                     target_dir: "str | Path | None" = None,
                     crontab_path: "str | Path | None" = None,
                     logs_dir: "str | Path | None" = None) -> dict[str, Any]:
    """Install/uninstall a scheduled ``fulcra-coord notify-inbox --agent`` job.

    The listener is PER-AGENT: identity (launchd label / plist basename / cron
    marker) is derived from ``agent_slug(agent)``, so co-located agents each get
    their own coexisting job instead of one clobbering the other. Install/uninstall
    touch ONLY the given agent's job — never another agent's.

    macOS -> launchd plist in ``target_dir`` (default ~/Library/LaunchAgents);
    other platforms -> a managed crontab line in ``crontab_path``. ``target_dir``
    / ``crontab_path`` / ``logs_dir`` are overridable so tests never touch the
    real scheduler or ~/Library. Idempotent, surgical uninstall, dry-run writes
    nothing. Cadence is per-agent-configurable (default 10 min). On macOS the
    plist carries a hardened PATH + log paths (#25); the ``logs_dir`` (default
    ~/Library/Logs/fulcra-coord) is created on install.

    Legacy migration: a pre-0.5.3 build wrote a single machine-global plist /
    bare cron marker. On install, if that legacy job watches THIS agent it is
    superseded by the new per-agent job (prevents this agent double-running); if
    it watches a DIFFERENT agent it is left for that agent to migrate on its own
    reinstall. Best-effort, never raises.
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
        # True when a legacy un-slugged job watching THIS agent was found and
        # (would be) superseded — surfaced in the dry-run output.
        "supersedes_legacy": False,
    }

    if scheduler_env.is_macos():
        base = Path(target_dir) if target_dir is not None else scheduler_env.launchagents_dir()
        logs = Path(logs_dir) if logs_dir is not None \
            else scheduler_env.default_logs_dir()
        plist = base / _plist_name_for(agent)
        if uninstall:
            plan["removes"].append(str(plist))
            if not dry_run and plist.exists():
                plist.unlink()
            return plan

        # Supersede a legacy un-slugged plist ONLY when it watches this agent —
        # otherwise it belongs to a different agent that migrates on its own.
        legacy = base / LEGACY_PLIST_NAME
        if legacy.exists() and _legacy_plist_agent(legacy) == agent:
            plan["supersedes_legacy"] = True
            plan["removes"].append(str(legacy))
            if not dry_run:
                _launchctl_unload(legacy)
                if legacy.exists():
                    legacy.unlink()

        plan["writes"].append(str(plist))
        if not dry_run:
            base.mkdir(parents=True, exist_ok=True)
            # Create the Logs dir so launchd's StandardOut/ErrorPath is writable
            # from the first tick (launchd will not create it for us).
            logs.mkdir(parents=True, exist_ok=True)
            plist.write_text(
                _plist_body(argv, agent, interval_sec, logs, _label_for(agent)))
        return plan

    # --- crontab fallback (Linux / other) ----------------------------------
    cron = Path(crontab_path) if crontab_path is not None else None
    if cron is None:
        base = Path(target_dir) if target_dir is not None else Path.home()
        cron = base / "fulcra-coord-listener-crontab.txt"
    existing = cron.read_text() if cron.is_file() else ""
    # _strip_managed_cron is agent-scoped: it removes this agent's per-agent
    # block AND a legacy un-slugged block that fronts this agent's command
    # (legacy supersede), leaving other agents' managed blocks untouched.
    stripped = _strip_managed_cron(existing, agent)
    if stripped != existing:
        plan["supersedes_legacy"] = LEGACY_CRON_MARKER in existing
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


def emit_message(message: str, *, title: str = "fulcra-coord") -> None:
    """Best-effort desktop notification of an arbitrary `message`.

    The general-purpose sibling of ``emit_notification`` (which formats a fixed
    inbox-count string). Used for the blocked-on-you alert
    ("⛔ <agent> needs you: <ask>") where the caller composes the full message.
    macOS -> osascript ``display notification``; elsewhere -> a stderr line that
    cron/launchd capture. Swallows EVERY error so a failed notification never
    breaks the scheduled tick (fail-safe contract). Pure side-effect."""
    try:
        if scheduler_env.is_macos():
            subprocess.run(
                ["osascript", "-e",
                 f'display notification {_osa_quote(message)} '
                 f'with title {_osa_quote(title)}'],
                capture_output=True, timeout=5, check=False,
            )
        else:
            print(f"[fulcra-coord] {message}", file=sys.stderr)
    except Exception:
        pass


def _osa_quote(s: str) -> str:
    """Quote a string for an AppleScript literal (double-quote delimited):
    backslash-escape backslashes and double-quotes so a directive count string
    can't break out of the osascript expression."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

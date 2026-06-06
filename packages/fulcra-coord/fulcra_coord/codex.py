"""Codex auto-integration (Gap 4): hooks.json merge + installer.

Codex is the one heavy-coder agent still on passive prose coordination, yet it
has deterministic lifecycle hooks. This wires them, mirroring
``install_claude_code``.

Why reuse the Claude Code script bodies: a Codex hook receives the same stdin
JSON shape (``session_id`` / ``transcript_path`` / ``cwd``) as a Claude Code
hook, so the SessionStart body is shared *verbatim* and PreCompact reuses the CC
body with one swap — the session-id env fallback is keyed on
``FULCRA_COORD_SESSION_KEY`` (the generic pointer key, see session_link.py)
instead of ``CLAUDE_CODE_SESSION_ID``, because Codex's native session-id env
differs. The CLI's session pointer keys on FULCRA_COORD_SESSION_KEY as its
generic fallback, so a Codex session that exports it gets the same
resume/checkpoint behaviour as Claude Code.

Which events — and which we deliberately omit:

  * ``SessionStart`` — surface in-flight / possibly-forgotten work.
  * ``PreCompact`` — ALWAYS checkpoint the session's task before context loss.
  * NO ``Stop``. Codex ``Stop`` fires every turn; parking the active task on every
    turn would thrash it between active/waiting. End-parking for Codex is instead
    delegated to the heartbeat reconciler (Gap 2), which sweeps stale active
    tasks on a cadence — the right backstop for an agent with no clean
    end-of-session boundary.

Contract mirrors the other installers: idempotent surgical merge into
``~/.codex/hooks.json`` (JSON, same shape as the Claude Code settings.json merge),
``dry_run`` writes nothing, ``uninstall`` removes only our managed entries,
fail-safe. Gap-1 command resolution bakes a callable invocation into the scripts.
The committed parity copies under adapters/codex/hooks keep the literal
placeholder.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import claude_code, cli_invocation
from .cli_invocation import PLACEHOLDER_ARGV

# SessionStart mostly matches Claude Code's (same stdin shape, cwd-driven), but
# Codex is the canonical reviewer in the review-router seed pool. If its startup
# hook publishes ordinary presence without the review capability, the router
# sees the right identity but rates it below-floor. Keep Codex findable for PR
# review requests by declaring review capability on every connect.
SESSION_START_SH = claude_code.SESSION_START_SH.replace(
    '"${FULCRA_COORD[@]}" connect >/dev/null 2>&1 &',
    '"${FULCRA_COORD[@]}" connect --can-review >/dev/null 2>&1 &',
)

# PreCompact reuses the CC body but keys the session-id env fallback on the
# generic FULCRA_COORD_SESSION_KEY rather than Claude Code's native env var.
PRE_COMPACT_SH = claude_code.PRE_COMPACT_SH.replace(
    "$CLAUDE_CODE_SESSION_ID", "$FULCRA_COORD_SESSION_KEY"
)

MANAGED_DIRNAME = "fulcra-coord-hooks"
_SCRIPTS = {
    "session-start.sh": SESSION_START_SH,
    "pre-compact.sh": PRE_COMPACT_SH,
}
# event name -> (script filename, matcher or None). Deliberately NO Stop.
_EVENTS: dict[str, tuple[str, "str | None"]] = {
    "SessionStart": ("session-start.sh", "startup|resume|clear|compact"),
    "PreCompact": ("pre-compact.sh", None),
}


def _codex_dir(target_dir: "str | Path | None") -> Path:
    return Path(target_dir) if target_dir is not None else Path.home() / ".codex"


def _is_managed(cmd: str) -> bool:
    return MANAGED_DIRNAME in cmd


def install_codex(*, uninstall: bool = False, dry_run: bool = False,
                  target_dir: "str | Path | None" = None) -> dict[str, Any]:
    """Install/uninstall the Codex coordination hooks.

    Materializes the SessionStart + PreCompact scripts under
    ``<codex>/fulcra-coord-hooks/`` and merges the two hook entries into
    ``<codex>/hooks.json`` (default ``~/.codex/``; ``target_dir`` overridable for
    tests). Idempotent, surgical uninstall, dry-run writes nothing.
    """
    codex_dir = _codex_dir(target_dir)
    hooks_path = codex_dir / "hooks.json"
    hooks_dir = codex_dir / MANAGED_DIRNAME
    plan: dict[str, Any] = {"hooks_file": str(hooks_path), "hooks_dir": str(hooks_dir),
                            "uninstall": uninstall, "scripts": [], "events": []}

    config: Any = {}
    if hooks_path.is_file():
        try:
            config = json.loads(hooks_path.read_text())
        except ValueError:
            # Unparseable JSON: back up the original bytes before overwriting so
            # the user's content is never silently destroyed (best-effort).
            if not dry_run:
                try:
                    bak = hooks_path.with_suffix(hooks_path.suffix + ".bak")
                    bak.write_bytes(hooks_path.read_bytes())
                except OSError:
                    pass
            config = {}
    if not isinstance(config, dict):
        config = {}

    existing_hooks = config.get("hooks")
    if not isinstance(existing_hooks, dict):
        existing_hooks = {}
        config["hooks"] = existing_hooks
    hooks = config.setdefault("hooks", {}) if not dry_run else dict(existing_hooks)

    # Strip our managed entries first (idempotent + clean uninstall), preserving
    # the user's own and any non-managed hooks within shared events.
    for event in _EVENTS:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            entries = []
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_hooks = [h for h in entry.get("hooks", []) if not _is_managed(h.get("command", ""))]
            if entry_hooks:
                kept.append({**entry, "hooks": entry_hooks})
            elif not entry.get("hooks"):
                kept.append(entry)
        if kept:
            hooks[event] = kept
        elif event in hooks:
            del hooks[event]

    if not uninstall:
        for event, (fname, matcher) in _EVENTS.items():
            cmd = str(hooks_dir / fname)
            plan["scripts"].append(cmd)
            plan["events"].append(event)
            entry: dict[str, Any] = {"hooks": [{"type": "command", "command": cmd}]}
            if matcher:
                entry["matcher"] = matcher
            hooks.setdefault(event, []).append(entry)

    if dry_run:
        plan["would_write"] = {**config, "hooks": hooks}
        return plan

    if not uninstall:
        # Gap 1 + C1: bake the resolved argv into the materialized scripts as a
        # shell-quoted bash-array body (space-safe), not a word-splittable string.
        argv = cli_invocation.resolve_cli_argv()
        plan["resolved_cli"] = cli_invocation.resolve_cli_command()
        substituted = cli_invocation.materialize_argv(argv)
        hooks_dir.mkdir(parents=True, exist_ok=True)
        for fname, body in _SCRIPTS.items():
            p = hooks_dir / fname
            p.write_text(body.replace(PLACEHOLDER_ARGV, substituted))
            p.chmod(0o755)

    config["hooks"] = hooks
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(config, indent=2) + "\n")
    return plan

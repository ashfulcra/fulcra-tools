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
import os
import shutil
import time
from pathlib import Path
from typing import Any

from . import claude_code, cli_invocation, wake
from .cli_invocation import PLACEHOLDER_ARGV
from .views import agent_slug

THREAD_AUTOMATION_INTERVAL_MIN_DEFAULT = 5
CODEX_WAKE_ENV = "FULCRA_COORD_CODEX_WAKE"

# SessionStart mostly matches Claude Code's (same stdin shape, cwd-driven), but
# Codex is the canonical reviewer in the review-router seed pool. If its startup
# hook publishes ordinary presence without the review capability, the router
# sees the right identity but rates it below-floor. Keep Codex findable for PR
# review requests by declaring review capability on every connect.
SESSION_START_SH = claude_code.SESSION_START_SH.replace(
    'CWD="$(printf \'%s\' "$INPUT" | python3 -c \'import sys,json;print(json.load(sys.stdin).get("cwd",""))\' 2>/dev/null)"',
    'CWD="$(printf \'%s\' "$INPUT" | python3 -c \'import sys,json;print(json.load(sys.stdin).get("cwd",""))\' 2>/dev/null)"\n'
    'SESSION_ID="$(printf \'%s\' "$INPUT" | python3 -c \'import sys,json;print(json.load(sys.stdin).get("session_id",""))\' 2>/dev/null)"',
).replace(
    'CONNECT_FLAGS=(__FULCRA_COORD_CONNECT_FLAGS__)\n'
    '"${FULCRA_COORD[@]}" connect "${CONNECT_FLAGS[@]}" >/dev/null 2>&1 &',
    # Re-arm Codex hooks + the per-agent inbox listener on every app start, THEN
    # connect with the review capability. The self-heal is the whole point on a
    # fresh Codex box where nobody ever ran `install-listener`: without a listener
    # Codex silently never hears directed work on the bus. Backgrounded + silenced
    # so it never blocks/slows boot; an old CLI without `ensure-codex-watch` simply
    # no-ops. `--no-connect` because the `connect --can-review` below already
    # refreshes presence — avoid a double-connect.
    '# Re-arm Codex hooks + the per-agent inbox listener on every app start.\n'
    '# Backgrounded + silenced; an old CLI without ensure-codex-watch simply no-ops.\n'
    '# A headless `codex exec` wake may run SessionStart too. It should refresh\n'
    '# hooks/listeners, but must not steal this app thread heartbeat by retargeting\n'
    '# the managed automation at its throwaway exec session id.\n'
    f'if [ -n "$SESSION_ID" ] && [ -z "${{{CODEX_WAKE_ENV}:-}}" ]; then\n'
    '  "${FULCRA_COORD[@]}" ensure-codex-watch --agent "$AGENT" --thread-id "$SESSION_ID" --no-connect >/dev/null 2>&1 &\n'
    'else\n'
    '  "${FULCRA_COORD[@]}" ensure-codex-watch --agent "$AGENT" --no-connect >/dev/null 2>&1 &\n'
    'fi\n'
    'CONNECT_FLAGS=(--can-review __FULCRA_COORD_CONNECT_FLAGS__)\n'
    '"${FULCRA_COORD[@]}" connect "${CONNECT_FLAGS[@]}" >/dev/null 2>&1 &',
).replace(
    # The shared CC body derives a `claude-code:*` fallback id when the CLI can't
    # resolve one (pre-handshake / old CLI). In a Codex hook that derived shape
    # must be `codex:*` so a fresh Codex box without a declared identity still
    # arms + watches the RIGHT (codex) agent's inbox — the slug the listener job
    # and `ensure-codex-watch --agent "$AGENT"` then key off.
    '[ -z "$AGENT" ] && AGENT="claude-code:${HOST}:${REPO}"',
    '[ -z "$AGENT" ] && AGENT="codex:${HOST}:${REPO}"',
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
                  target_dir: "str | Path | None" = None,
                  roles: "list[str] | None" = None) -> dict[str, Any]:
    """Install/uninstall the Codex coordination hooks.

    Materializes the SessionStart + PreCompact scripts under
    ``<codex>/fulcra-coord-hooks/`` and merges the two hook entries into
    ``<codex>/hooks.json`` (default ``~/.codex/``; ``target_dir`` overridable for
    tests). Idempotent, surgical uninstall, dry-run writes nothing.
    """
    codex_dir = _codex_dir(target_dir)
    hooks_path = codex_dir / "hooks.json"
    hooks_dir = codex_dir / MANAGED_DIRNAME
    flags_path = claude_code._connect_flags_path(codex_dir)
    plan: dict[str, Any] = {"hooks_file": str(hooks_path), "hooks_dir": str(hooks_dir),
                            "connect_flags_file": str(flags_path),
                            "uninstall": uninstall, "scripts": [], "events": []}
    _effective_can_review, effective_roles = claude_code._effective_connect_flags(
        flags_path, roles=roles, persist=(not uninstall and roles is not None),
        dry_run=dry_run)
    plan["connect_flags"] = claude_code.materialize_connect_flags(
        roles=effective_roles)

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
            p.write_text(claude_code.materialize_script(
                body, substituted, roles=effective_roles))
            p.chmod(0o755)
    else:
        try:
            flags_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    config["hooks"] = hooks
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(config, indent=2) + "\n")
    return plan


# Host wake-exec adapter (--with-wake / ensure-codex-watch --with-wake).
#
# Core wake.py is platform-neutral; the concrete Codex command lives here, in the
# Codex adopter layer, and is written to the operator-owned wake.json file. This
# mirrors Claude Code's --with-wake flow but uses `codex exec` so the listener can
# wake a headless Codex worker instead of only preparing the next SessionStart.

def _default_wake_prompt(agent: str) -> str:
    return (f"BUS WAKE: you are {agent}. Use the fulcra-coord CLI as the bus "
            "source of truth: run `fulcra-coord inbox --agent "
            f"{agent}` and `fulcra-coord resume --agent {agent}`. Do not look "
            "for a local tasks/ directory. Act only on directives/verdicts for "
            "this agent, close loops with evidence, then exit.")


def default_wake_entry(agent: str) -> dict[str, Any]:
    codex_bin = shutil.which("codex") or "codex"
    return {
        "cmd": ["/usr/bin/env", f"{CODEX_WAKE_ENV}=1", codex_bin, "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                _default_wake_prompt(agent)],
        "cwd": str(Path.cwd()),
        "min_interval_min": 15,
        "max_runtime_s": 900,
        "enabled": True,
    }


def install_wake(agent: str, *, uninstall: bool = False,
                 dry_run: bool = False) -> dict[str, Any]:
    """Merge (or remove) this Codex agent's wake entry in wake.json.

    Existing operator-tuned entries are preserved; uninstall removes only this
    agent's key. A corrupt wake.json is backed up before replacement, matching
    the Claude Code adopter semantics.
    """
    path = wake._wake_config_path()
    plan: dict[str, Any] = {"config": str(path), "agent": agent,
                            "uninstall": uninstall, "dry_run": dry_run,
                            "preserved": False, "would_write": None}

    cfg: Any = {}
    corrupt_bytes: "bytes | None" = None
    if path.is_file():
        try:
            cfg = json.loads(path.read_text())
        except ValueError:
            corrupt_bytes = path.read_bytes()
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}

    if uninstall:
        cfg.pop(agent, None)
    elif agent in cfg and isinstance(cfg[agent], dict):
        plan["preserved"] = True
    else:
        cfg[agent] = default_wake_entry(agent)

    plan["would_write"] = cfg
    if dry_run:
        return plan

    if corrupt_bytes is not None:
        try:
            path.with_suffix(path.suffix + ".bak").write_bytes(corrupt_bytes)
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return plan


# Codex app thread automation adapter.
#
# Session hooks and launchd listeners are not enough for an already-open Codex
# app thread: Codex's app-native heartbeat automation is the layer that wakes
# THIS conversation on a cadence. The SessionStart hook has the current
# session/thread id, so it can seed a managed automation here.

def _codex_home() -> Path:
    return Path.home() / ".codex"


def _automation_id(agent: str) -> str:
    return f"fulcra-coord-task-listener-{agent_slug(agent)}"


def _automation_path(agent: str) -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME") or _codex_home())
    aid = _automation_id(agent)
    return codex_home / "automations" / aid / "automation.toml"


def _toml_str(s: str) -> str:
    return json.dumps(s)


def _automation_prompt(agent: str) -> str:
    return (
        f"Continue the fulcra-coord maintainer/listener loop for `{agent}`. "
        f"Run `fulcra-coord inbox --agent {agent} --format json`, "
        "`fulcra-coord board --format json`, `fulcra-coord needs-me`, and check "
        "open PR/reviewer state when this workspace has a GitHub remote. If the "
        "inbox, board, review delivery, or summaries look stale or inconsistent, "
        "run `fulcra-coord health`; if this host's scheduled reconcile is fresh, "
        "trust that heartbeat and re-check reads, and only run `fulcra-coord "
        "reconcile` yourself when the scheduled heartbeat is missing/stale. If "
        "there is actionable work for this agent, continue handling it end-to-end, "
        "route unrouted PRs via `fulcra-coord request-review` after the reviewer "
        "role/dedup routing change has landed — never raw tells for review work, "
        "so duplicate suppression applies — notify reviewer inboxes, update bus "
        "tasks with evidence, and stop only when blocked on user input or external "
        "review. Also verify the host launchd listener for this agent is still "
        "loaded, exiting 0, and writing per-agent listener breadcrumbs; run "
        f"`fulcra-coord ensure-codex-watch --agent {agent}` if it needs to be "
        "re-armed."
    )


def _parse_simple_toml_fields(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = [p.strip() for p in line.split("=", 1)]
        try:
            out[key] = json.loads(val)
        except Exception:
            try:
                out[key] = int(val)
            except ValueError:
                out[key] = val.strip('"')
    return out


def install_thread_automation(
    agent: str, thread_id: str, *,
    interval_min: int = THREAD_AUTOMATION_INTERVAL_MIN_DEFAULT,
    uninstall: bool = False, dry_run: bool = False,
) -> dict[str, Any]:
    """Install/update the Codex thread heartbeat automation for this agent.

    The file format mirrors Codex app's automation.toml. We preserve the
    existing created_at when updating our managed id, and touch no other
    automations. Without a thread id, callers should skip this entirely.
    """
    aid = _automation_id(agent)
    path = _automation_path(agent)
    plan: dict[str, Any] = {
        "id": aid, "path": str(path), "agent": agent, "thread_id": thread_id,
        "interval_min": max(1, int(interval_min or THREAD_AUTOMATION_INTERVAL_MIN_DEFAULT)),
        "uninstall": uninstall, "dry_run": dry_run, "would_write": None,
    }
    if uninstall:
        if not dry_run and path.exists():
            try:
                path.unlink()
                path.parent.rmdir()
            except OSError:
                pass
        return plan

    now_ms = int(time.time() * 1000)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = _parse_simple_toml_fields(path.read_text())
        except OSError:
            existing = {}
    created_at = existing.get("created_at") if isinstance(existing.get("created_at"), int) else now_ms
    rrule = f"FREQ=MINUTELY;INTERVAL={plan['interval_min']}"
    fields: dict[str, Any] = {
        "version": 1,
        "id": aid,
        "kind": "heartbeat",
        "name": "Fulcra Coord task listener",
        "prompt": _automation_prompt(agent),
        "status": "ACTIVE",
        "rrule": rrule,
        "target_thread_id": thread_id,
        "created_at": created_at,
        "updated_at": now_ms,
    }
    body = (
        f"version = {fields['version']}\n"
        f"id = {_toml_str(fields['id'])}\n"
        f"kind = {_toml_str(fields['kind'])}\n"
        f"name = {_toml_str(fields['name'])}\n"
        f"prompt = {_toml_str(fields['prompt'])}\n"
        f"status = {_toml_str(fields['status'])}\n"
        f"rrule = {_toml_str(fields['rrule'])}\n"
        f"target_thread_id = {_toml_str(fields['target_thread_id'])}\n"
        f"created_at = {fields['created_at']}\n"
        f"updated_at = {fields['updated_at']}\n"
    )
    plan["would_write"] = body
    if dry_run:
        return plan
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return plan

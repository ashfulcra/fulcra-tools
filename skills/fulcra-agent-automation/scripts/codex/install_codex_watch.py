#!/usr/bin/env python3
"""coord-first Codex watch installer (fulcra-agent-automation).

Standalone (Python 3.10+ stdlib only). Ports the hardened legacy Codex
adapter mechanics onto the coord engine, in three layers:

  * ``hooks.json`` merge — SessionStart (matcher ``startup|resume|clear|compact``)
    + PreCompact, same entry shape as Claude Code settings.json. Deliberately
    NO Stop hook: Codex Stop fires every turn, and parking the active task on
    every turn would thrash it between active/waiting.
  * managed scripts — materialized under ``<codex>/fulcra-agent-hooks/``.
    SessionStart emits a bounded resume brief + briefing as additionalContext;
    PreCompact backgrounds ``coord-engine continuity park``. Both degrade
    silently (exit 0) when coord-engine is not on PATH.
  * app-thread automation — writes the coord-first ``COORD_WATCH_PROMPT``
    to ``<codex>/automations/coord-watch-<agent-slug>/automation.toml``.
    This is the durable coord-first replacement for the legacy watch prompt.
    The target thread id is taken from ``--thread-id``, else preserved from
    our existing managed automation; with neither, the automation write is
    deferred and the SessionStart hook seeds it (once, only while absent) from
    the first app session's id — so an already-armed watch thread can never be
    stolen by a later (possibly headless) session.

DELIBERATELY OMITTED (security scope ruling): the legacy ``wake.json``
host-wake layer. It spawns headless ``codex exec`` sessions with
``--dangerously-bypass-approvals-and-sandbox`` — a consent-gated,
security-sensitive surface not shipped in this pass. The existing coord
listener already covers wake.

COEXISTENCE + MIGRATION: this installer keys everything on the marker
``fulcra-agent-hooks`` and automation id prefix ``coord-watch-``. Two other
generations may exist on a real host:

  * PRE-COORD2 LEGACY (never touched) — managed scripts dir ``fulcra-coord-hooks``
    and automation ids ``fulcra-coord-task-listener-*``. Neither current marker
    is a substring of a legacy one (``fulcra-agent-hooks`` vs ``fulcra-coord-hooks``;
    ``coord-watch-`` vs ``fulcra-coord-task-listener-``) in either direction, so
    this installer's dedupe/uninstall can never match — let alone modify — a
    pre-coord2 legacy hooks entry or its automation directory.
  * COORD2-ERA (migrated in place) — the immediately-prior build of THIS
    installer used dir ``fulcra-coord2-hooks`` and prefix ``coord2-watch-``.
    Those old names are recognized (``LEGACY_MANAGED_DIRNAME`` /
    ``LEGACY_AUTOMATION_ID_PREFIX`` / ``LEGACY_MANAGED_MARKER``) so a re-run
    converges a coord2-era host to the new names: old hooks-dir removed, old
    hooks.json entries stripped, old automation dir replaced (its target thread
    id + created_at preserved). Uninstall removes BOTH generations. Fresh
    install writes the new names only. The pre-coord2 and coord2-era legacy
    names appear in this file only as these recognition constants + comments.

CLI:
  python3 install_codex_watch.py <team> <agent>
      [--codex-dir DIR] [--thread-id ID] [--interval-minutes N]
      [--uninstall] [--dry-run]

``--dry-run`` prints the would-be hooks.json + automation body + prompt and
writes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any

MANAGED_DIRNAME = "fulcra-agent-hooks"
AUTOMATION_ID_PREFIX = "coord-watch-"
# coord2-era names of THIS installer, recognized for in-place migration only.
LEGACY_MANAGED_DIRNAME = "fulcra-coord2-hooks"
LEGACY_AUTOMATION_ID_PREFIX = "coord2-watch-"
# Codex Desktop currently has no documented inbound webhook that resumes an
# exact existing app task. Keep a low-frequency safety net; supported push
# adapters (for example OpenClaw) should use the model-free listener instead.
WATCH_INTERVAL_MIN = 30
# Marker carried once in hooks.json (as a trailing shell comment on the
# SessionStart command — inert under sh -c) and as the automation prompt's
# first line, so re-runs converge and audits can grep one string.
MANAGED_MARKER = "coord watch (managed by fulcra-agent-automation/scripts/codex)"
LEGACY_MANAGED_MARKER = "coord2 watch (managed by fulcra-agent-automation/scripts/codex)"

# event name -> (script filename, matcher or None). Deliberately NO Stop.
_EVENTS: "dict[str, tuple[str, str | None]]" = {
    "SessionStart": ("session-start.sh", "startup|resume|clear|compact"),
    "PreCompact": ("pre-compact.sh", None),
}

COORD_WATCH_PROMPT = """\
[coord watch — managed by fulcra-agent-automation/scripts/codex; do not hand-edit]
You are {agent} on coord team {team}. Apply the fulcra-agent-automation tick contract:
resume continuity, run `coord-engine briefing {team} --agent {agent}` once, and handle every
surfaced item end-to-end. A degraded section is not clear: use its documented targeted
fallback. For reviews, write and verify the exact required verdict before acking. Snapshot
material work, refresh presence/held-role leases, then report last. If nothing is actionable,
the final output is exactly WATCH_OK.
"""

SESSION_START_SH = """\
#!/bin/bash
# coord SessionStart hook (Codex) — bounded resume brief + briefing context.
# Managed by fulcra-agent-automation/scripts/codex/install_codex_watch.py.
# Output is bounded: an unbounded board dump is a known context-flooding
# failure. Degrades silently (exit 0) when coord-engine is not on PATH.
#
# __TEAM__/__AGENT__/__HOOKS_DIR__/__CODEX_DIR__/__AUTOMATION_TOML__ are rendered
# by install_codex_watch.py as SHELL-QUOTED literals (shlex.quote), so these
# bare (unquoted) assignments round-trip any id/path byte verbatim — a raw
# replace into a double-quoted context would let an id like `bad"agent` break
# out of the string and inject shell.
set +e
TEAM=__TEAM__; AGENT=__AGENT__
HOOKS_DIR=__HOOKS_DIR__; CODEX_DIR=__CODEX_DIR__
export FULCRA_COORD_AGENT="$AGENT"
INPUT="$(cat 2>/dev/null)"
SESSION_ID="$(printf '%s' "$INPUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null)"
# Seed the coord watch automation with this app thread's id — only while the
# managed automation does not exist yet, so a later session (e.g. a headless
# exec run) can never steal an already-armed watch thread. Backgrounded +
# silenced so it never blocks or slows session start.
if [ -n "$SESSION_ID" ] && [ ! -f __AUTOMATION_TOML__ ]; then
  python3 "$HOOKS_DIR/install_codex_watch.py" "$TEAM" "$AGENT" \\
    --codex-dir "$CODEX_DIR" --thread-id "$SESSION_ID" >/dev/null 2>&1 &
fi
command -v coord-engine >/dev/null 2>&1 || exit 0
BRIEF="$(coord-engine continuity resume "$TEAM" "$AGENT" 2>/dev/null | head -25)"
BRIEFING="$(coord-engine briefing "$TEAM" --agent "$AGENT" 2>/dev/null | head -60)"
[ -z "$BRIEF$BRIEFING" ] && exit 0
python3 - "$BRIEF" "$BRIEFING" <<'PYEOF'
import json, sys
brief, briefing = sys.argv[1], sys.argv[2]
ctx = "coord resume brief:\\n" + brief + "\\n\\ncoord briefing:\\n" + briefing
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart", "additionalContext": ctx[:4000]}}))
PYEOF
exit 0
"""

PRE_COMPACT_SH = """\
#!/bin/bash
# coord park-on-context-loss hook (Codex PreCompact).
# Managed by fulcra-agent-automation/scripts/codex/install_codex_watch.py.
# Backgrounded, never blocks; degrades silently if coord-engine is absent.
# __TEAM__/__AGENT__ are rendered as shlex.quote'd literals (see session-start),
# so these bare assignments round-trip any id byte without shell injection.
set +e
TEAM=__TEAM__; AGENT=__AGENT__
export FULCRA_COORD_AGENT="$AGENT"
command -v coord-engine >/dev/null 2>&1 || exit 0
coord-engine continuity park "$TEAM" --agent "$AGENT" \\
  --objective "context-loss park ($(date -u +%Y-%m-%dT%H:%MZ))" >/dev/null 2>&1 &
exit 0
"""


def _agent_slug(agent: str) -> str:
    """Filesystem-safe slug: collapse non-[a-z0-9-_.] runs to '-', lowercased."""
    slug = re.sub(r"[^a-z0-9\-_.]+", "-", agent.lower()).strip("-")
    return slug or "agent"


def _is_managed(cmd: str) -> bool:
    """Recognize a hooks.json command as OURS — current or coord2-era. Both
    dirnames are distinct non-substrings of the pre-coord2 legacy
    ``fulcra-coord-hooks``, so a pre-coord2 entry is never matched."""
    return MANAGED_DIRNAME in cmd or LEGACY_MANAGED_DIRNAME in cmd


def _atomic_write(path: Path, text: str, mode: "int | None" = None) -> None:
    """Write-then-rename so a concurrently-executing script keeps its inode."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, path)


def _load_hooks_config(path: Path, dry_run: bool) -> dict:
    """Parse hooks.json; a corrupt file is backed up (.bak) before replacement
    so the user's content is never silently destroyed (legacy hardening)."""
    config: Any = {}
    if path.is_file():
        try:
            config = json.loads(path.read_text())
        except ValueError:
            if not dry_run:
                try:
                    path.with_suffix(path.suffix + ".bak").write_bytes(path.read_bytes())
                except OSError:
                    pass
            config = {}
    return config if isinstance(config, dict) else {}


def _strip_managed(hooks: dict) -> None:
    """Remove only OUR entries (marker-keyed), preserving foreign entries —
    including non-dict oddities — within shared events."""
    for event in _EVENTS:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            entry_hooks = [h for h in entry.get("hooks", [])
                           if not (isinstance(h, dict) and _is_managed(h.get("command", "")))]
            if entry_hooks:
                kept.append({**entry, "hooks": entry_hooks})
            elif not entry.get("hooks"):
                kept.append(entry)
        if kept:
            hooks[event] = kept
        elif event in hooks:
            del hooks[event]


def _automation_path(codex_dir: Path, agent: str) -> Path:
    aid = AUTOMATION_ID_PREFIX + _agent_slug(agent)
    return codex_dir / "automations" / aid / "automation.toml"


def _legacy_automation_path(codex_dir: Path, agent: str) -> Path:
    """coord2-era automation path, recognized for migration/uninstall only."""
    aid = LEGACY_AUTOMATION_ID_PREFIX + _agent_slug(agent)
    return codex_dir / "automations" / aid / "automation.toml"


def _remove_automation(path: Path) -> None:
    """Unlink an automation.toml and prune its now-empty id directory."""
    try:
        path.unlink()
        path.parent.rmdir()
    except OSError:
        pass


def _toml_str(s: str) -> str:
    return json.dumps(s)


def _parse_simple_toml_fields(text: str) -> dict:
    out: dict = {}
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


def install_automation(team: str, agent: str, codex_dir: Path, *,
                       thread_id: "str | None", uninstall: bool,
                       dry_run: bool, interval_minutes: int = WATCH_INTERVAL_MIN) -> dict:
    """Install/update our coord watch automation. Preserves created_at and
    (absent an explicit --thread-id) the existing target thread on re-runs;
    with no thread id at all, defers (SessionStart hook seeds it later)."""
    path = _automation_path(codex_dir, agent)
    legacy_path = _legacy_automation_path(codex_dir, agent)
    aid = path.parent.name
    plan: dict = {"id": aid, "path": str(path), "deferred": False}
    if uninstall:
        # Remove BOTH generations' automation dirs.
        if not dry_run:
            _remove_automation(path)
            _remove_automation(legacy_path)
        plan["removed"] = True
        return plan

    # Preserve created_at + the armed target thread across re-runs. Read from
    # our current automation if present, else from a coord2-era one (migration)
    # so an already-armed watch thread is carried over, never re-seeded/stolen.
    existing: dict = {}
    src = path if path.is_file() else (legacy_path if legacy_path.is_file() else None)
    if src is not None:
        try:
            existing = _parse_simple_toml_fields(src.read_text())
        except OSError:
            existing = {}
    thread = thread_id or existing.get("target_thread_id") or ""
    if not thread:
        plan["deferred"] = True
        plan["note"] = ("no thread id yet: pass --thread-id, or the SessionStart "
                        "hook seeds it on the next Codex app session start")
        return plan

    now_ms = int(time.time() * 1000)
    created = existing.get("created_at")
    created_at = created if isinstance(created, int) else now_ms
    prompt = COORD_WATCH_PROMPT.format(team=team, agent=agent)
    body = (
        "version = 1\n"
        f"id = {_toml_str(aid)}\n"
        'kind = "heartbeat"\n'
        f"name = {_toml_str('coord watch (' + agent + ')')}\n"
        f"prompt = {_toml_str(prompt)}\n"
        'status = "ACTIVE"\n'
        f'rrule = "FREQ=MINUTELY;INTERVAL={interval_minutes}"\n'
        f"target_thread_id = {_toml_str(str(thread))}\n"
        f"created_at = {created_at}\n"
        f"updated_at = {now_ms}\n"
    )
    plan["target_thread_id"] = str(thread)
    plan["would_write"] = body
    if legacy_path.exists():
        plan["migrated_from"] = str(legacy_path.parent.name)
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, body)
        # Migration: drop the coord2-era automation dir now that the new one is
        # armed with its preserved thread — zero orphans.
        if legacy_path.resolve() != path.resolve():
            _remove_automation(legacy_path)
    return plan


def install(team: str, agent: str, *, codex_dir: Path,
            thread_id: "str | None" = None, uninstall: bool = False,
            dry_run: bool = False,
            interval_minutes: int = WATCH_INTERVAL_MIN) -> dict:
    if interval_minutes < 1:
        raise ValueError("interval_minutes must be >= 1")
    hooks_path = codex_dir / "hooks.json"
    hooks_dir = codex_dir / MANAGED_DIRNAME
    legacy_hooks_dir = codex_dir / LEGACY_MANAGED_DIRNAME
    plan: dict = {"hooks_file": str(hooks_path), "hooks_dir": str(hooks_dir),
                  "uninstall": uninstall, "dry_run": dry_run,
                  "events": [], "scripts": []}

    config = _load_hooks_config(hooks_path, dry_run)
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    config["hooks"] = hooks
    _strip_managed(hooks)

    if not uninstall:
        for event, (fname, matcher) in _EVENTS.items():
            cmd = str(hooks_dir / fname)
            if event == "SessionStart":
                cmd = f"{cmd}  # {MANAGED_MARKER}"
            entry: dict = {"hooks": [{"type": "command", "command": cmd}]}
            if matcher:
                entry["matcher"] = matcher
            hooks.setdefault(event, []).append(entry)
            plan["events"].append(event)
            plan["scripts"].append(str(hooks_dir / fname))

    plan["automation"] = install_automation(
        team, agent, codex_dir, thread_id=thread_id,
        uninstall=uninstall, dry_run=dry_run,
        interval_minutes=interval_minutes)

    if dry_run:
        plan["would_write_hooks_json"] = config
        if not uninstall:
            plan["prompt"] = COORD_WATCH_PROMPT.format(team=team, agent=agent)
        return plan

    if uninstall:
        # Remove BOTH generations' managed hooks dirs.
        shutil.rmtree(hooks_dir, ignore_errors=True)
        shutil.rmtree(legacy_hooks_dir, ignore_errors=True)
    else:
        automation_toml = _automation_path(codex_dir, agent)
        # Every value entering rendered shell source is shlex.quote'd (the
        # Claude-installer discipline): the templates carry the tokens in
        # UNQUOTED positions, so an operator id/path like `bad"agent`, `a$b`,
        # or one with spaces/`/` round-trips verbatim instead of breaking out
        # of a double-quoted assignment and injecting shell.
        subs = {"__TEAM__": shlex.quote(team), "__AGENT__": shlex.quote(agent),
                "__HOOKS_DIR__": shlex.quote(str(hooks_dir)),
                "__CODEX_DIR__": shlex.quote(str(codex_dir)),
                "__AUTOMATION_TOML__": shlex.quote(str(automation_toml))}
        hooks_dir.mkdir(parents=True, exist_ok=True)
        for fname, body in (("session-start.sh", SESSION_START_SH),
                            ("pre-compact.sh", PRE_COMPACT_SH)):
            for k, v in subs.items():
                body = body.replace(k, v)
            _atomic_write(hooks_dir / fname, body, mode=0o755)
        # Self-copy so the SessionStart hook can (re)seed the automation.
        me = Path(__file__).resolve()
        dest = hooks_dir / "install_codex_watch.py"
        if not (dest.exists() and dest.resolve() == me):
            _atomic_write(dest, me.read_text(), mode=0o755)
        # Migration: drop the coord2-era managed hooks dir now that the new one
        # is materialized — zero orphans on a converged host.
        if legacy_hooks_dir.resolve() != hooks_dir.resolve():
            shutil.rmtree(legacy_hooks_dir, ignore_errors=True)

    if not (uninstall and not hooks_path.exists()):
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(hooks_path, json.dumps(config, indent=2) + "\n")
    return plan


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        description="Install the coord-first Codex watch (hooks + automation).")
    p.add_argument("team")
    p.add_argument("agent")
    p.add_argument("--codex-dir", default=None,
                   help="Codex home (default ~/.codex); overridable for tests")
    p.add_argument("--thread-id", default=None,
                   help="app thread id for the watch automation (else preserved "
                        "from the existing managed automation, else hook-seeded)")
    p.add_argument("--interval-minutes", type=int, default=WATCH_INTERVAL_MIN,
                   help="Codex safety-net cadence (default 30; push-capable "
                        "harnesses should use the model-free listener)")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    codex_dir = (Path(args.codex_dir).expanduser() if args.codex_dir
                 else Path.home() / ".codex")
    plan = install(args.team, args.agent, codex_dir=codex_dir,
                   thread_id=args.thread_id, uninstall=args.uninstall,
                   dry_run=args.dry_run,
                   interval_minutes=args.interval_minutes)
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

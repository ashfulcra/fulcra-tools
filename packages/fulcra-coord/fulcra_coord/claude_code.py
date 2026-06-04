"""Claude Code auto-integration: hook-script templates + installer.

Hook-script contents are the single source of truth here (shipped in the wheel).
`install_claude_code` materializes them to ~/.claude/fulcra-coord-hooks/ and wires
settings.json. Committed copies under adapters/claude-code/hooks/ are kept in sync
by a parity test for repo readability.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cli_invocation import PLACEHOLDER_ARGV  # re-exported for adapter/test use

SESSION_START_SH = r'''#!/usr/bin/env bash
# fulcra-coord SessionStart hook — surface in-flight + possibly-forgotten work.
# Fail-safe: any error -> exit 0, inject nothing, never block the session.
set +e
INPUT="$(cat 2>/dev/null)"
CWD="$(printf '%s' "$INPUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("cwd",""))' 2>/dev/null)"
[ -z "$CWD" ] && CWD="$PWD"
HOST="$(hostname -s 2>/dev/null || echo host)"
REPO="$(basename "$CWD")"
STALE_HOURS="${FULCRA_COORD_STALE_HOURS:-2}"
# Resolved at install time (Gap 1) so the hook works under uv-tool / source
# installs where a bare `fulcra-coord` is not on PATH. A bash ARRAY (not a
# string) so a resolved argv[0] containing a space (e.g. an interpreter under
# "~/Library/Application Support/") survives intact under `"${FULCRA_COORD[@]}"`
# expansion — an unquoted string would word-split it into broken tokens (C1).
FULCRA_COORD=(__FULCRA_COORD_ARGV__)

# Resolve the agent id through the CLI so EVERY section agrees on "who am I"
# (I-2). inbox/needs-me already resolve via identity.resolve_agent (per-cwd
# persisted id), so the banner's "mine" filter, title, and resume hint must use
# the SAME resolution or they diverge the moment a stable id is declared with
# `identity set` — the shell-derived claude-code:<host>:<repo> would then differ
# from the declared id and the banner would show the wrong agent's open work.
# Fail-safe: an old/missing CLI yields empty -> fall back to the shell-derived id
# (the same shape resolve_agent derives), so the hook still works pre-handshake.
AGENT="$("${FULCRA_COORD[@]}" identity --format json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("agent",""))' 2>/dev/null)"
[ -z "$AGENT" ] && AGENT="claude-code:${HOST}:${REPO}"

JSON="$("${FULCRA_COORD[@]}" status --format json 2>/dev/null)"
[ -z "$JSON" ] && exit 0

# Report presence on connect (situational awareness): record this agent's current
# workstream(s) on the bus so `agents`/`presence` show what it's working on even
# when it owns no active task. `connect` auto-derives workstreams from this
# agent's open tasks. Backgrounded + silenced — best-effort, never blocks or
# delays session start; a missing/old CLI without `connect` simply no-ops.
"${FULCRA_COORD[@]}" connect >/dev/null 2>&1 &

# Directives addressed to this agent. status JSON may not carry `assignee`, so
# we ask the inbox command directly (fail-safe: empty/missing -> no section).
# This is the only extra call; it is silent and never blocks the session.
# Deliberately NO --agent: passing it is highest-precedence in resolve_agent and
# would OVERRIDE a persisted (`identity set`) or $FULCRA_COORD_AGENT identity,
# so directives addressed to a declared id would be missed. Letting inbox resolve
# its own agent honors the declared identity and falls back to the same derived
# "claude-code:${HOST}:${REPO}" id when none is set (I1).
INBOX="$("${FULCRA_COORD[@]}" inbox --format json 2>/dev/null)"

# What is blocked on the HUMAN — the situational-awareness banner that LEADS the
# injected context. Deliberately NO --human flag: the CLI resolves the operator's
# handle from $FULCRA_COORD_HUMAN / persisted config / the 'human' default, so we
# never hardcode a name here. Fail-safe: a missing/old CLI that lacks needs-me
# yields empty and simply omits the section.
NEEDSME="$("${FULCRA_COORD[@]}" needs-me --format json 2>/dev/null)"

CONTEXT="$(JSON="$JSON" INBOX="$INBOX" NEEDSME="$NEEDSME" AGENT="$AGENT" STALE_HOURS="$STALE_HOURS" FULCRA_COORD="${FULCRA_COORD[*]}" python3 - <<'PY' 2>/dev/null
import sys, json, os, datetime, shlex
agent = os.environ.get("AGENT","")
stale_h = float(os.environ.get("STALE_HOURS","2"))
try:
    d = json.loads(os.environ.get("JSON",""))
except Exception:
    sys.exit(0)
try:
    inbox = (json.loads(os.environ.get("INBOX","")) or {}).get("inbox", [])
except Exception:
    # Inbox is an optional add-on surface; a missing/old CLI that lacks the
    # subcommand must not break the in-flight+stale section, so default empty.
    inbox = []
try:
    _nm = json.loads(os.environ.get("NEEDSME","")) or {}
    needsme = _nm.get("items", [])
    # Upcoming = future-not_before asks the human cannot act on yet. They must
    # NEVER inflate the BLOCKED ON YOU headline count (the whole point of the
    # not_before gate); they only add one muted "+N upcoming" line. An old CLI
    # omits the key -> empty, so the banner degrades to the prior shape.
    upcoming = _nm.get("upcoming", [])
except Exception:
    # Same fail-safe contract as inbox: an old CLI without needs-me omits the
    # blocked-on-you banner rather than breaking the rest of the injection.
    needsme = []
    upcoming = []
active = d.get("active", []) or []
def age_hours(ts):
    try:
        t = datetime.datetime.fromisoformat(ts.replace("Z","+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - t).total_seconds()/3600.0
    except Exception:
        # Missing/corrupt timestamp -> treat as maximally stale, not fresh.
        # Returning 0.0 here would mask an active task that lost its clock,
        # which is exactly the "possibly-forgotten" case we want to surface.
        return float('inf')
mine = [t for t in active if t.get("owner_agent")==agent]
stale = [t for t in active if t.get("status")=="active" and age_hours(t.get("updated_at",""))>=stale_h]
if not mine and not stale and not inbox and not needsme and not upcoming:
    sys.exit(0)
lines = []
# LEAD with what's blocked on the human — the north-star situational-awareness
# surface — before any in-flight / directive / stale section. The headline
# counts ONLY due-now items.
if needsme:
    lines.append("⛔ BLOCKED ON YOU (%d):" % len(needsme))
    for it in needsme:
        ask = (it.get("blocked_on") or it.get("next_action") or "").strip()
        frm = it.get("owner_agent") or "?"
        lines.append("  %s — %s (from %s)" % (it.get("id",""), it.get("title",""), frm))
        if ask:
            lines.append("      needs: %s" % ask)
# Muted tail: how many not-yet-actionable asks are queued behind the plate.
# Deliberately a bare count (not the items) so it informs without nagging.
# BUG 9: hoisted OUT of the `if needsme:` block so a FUTURE-ONLY plate (no
# due-now items but upcoming asks) still surfaces this line instead of showing
# nothing. When due-now items exist it sits under the headline; otherwise it
# stands alone.
if upcoming:
    lines.append("  … (+%d upcoming)" % len(upcoming))
# M-1: a self-filed `block --on-user` task is owned by the agent AND appears in
# needs-me, so without this it would show BOTH in the ⛔ BLOCKED ON YOU banner
# above and again under "open work". Seed `seen` with the needs-me ids and drop
# those from `mine` BEFORE the header/resume checks so such a task is shown once
# (in the banner) and never produces an empty "open work" header below.
seen = {it.get("id") for it in needsme if it.get("id")}
mine = [t for t in mine if t['id'] not in seen]
# The shared-bus section header only when there's bus content to show under it
# (in-flight, stale, or directives) — otherwise a lone blocked-on-you banner
# would carry an empty "open work" header.
if mine or stale or inbox:
    lines.append("Fulcra coordination — open work on the shared bus:")
for t in mine:
    lines.append(f"  [{t.get('status','?').upper()}] {t['id']} — {t.get('title','')}")
    if t.get("next_action"):
        lines.append(f"      next: {t['next_action']}")
seen |= {t['id'] for t in mine}
extra = [t for t in stale if t['id'] not in seen]
if extra:
    lines.append("  ⚠ Possibly-forgotten (active, no recent update):")
    for t in extra:
        lines.append(f"      {t['id']} — {t.get('title','')} (agent {t.get('owner_agent','?')})")
if inbox:
    lines.append("  📥 Directives for you:")
    for it in inbox:
        frm = it.get("owner_agent") or it.get("from") or "?"
        lines.append(f"      {it.get('id','')} — {it.get('title','')} (from {frm})")
        if it.get("next_action"):
            lines.append(f"          next: {it['next_action']}")
if mine or stale or inbox:
    # BUG 5: $FULCRA_COORD is the joined resolved argv (e.g. an interpreter path
    # under "Application Support", or a path containing shell metacharacters).
    # Embedding it RAW into a suggested command let a metacharacter break / inject
    # into the hint a human might copy-paste. shlex.quote keeps it a single safe
    # token; the agent id is a derived slug but quote it too for symmetry/safety.
    _fc = shlex.quote(os.environ.get("FULCRA_COORD","fulcra-coord"))
    lines.append("  To resume: "+_fc+" update <id> --status active --agent "+shlex.quote(agent))
print("\n".join(lines))
PY
)"
[ -z "$CONTEXT" ] && exit 0

# Title = first of my active tasks, if any.
TITLE="$(JSON="$JSON" AGENT="$AGENT" python3 - <<'PY' 2>/dev/null
import sys,json,os
agent=os.environ.get("AGENT","")
try: d=json.loads(os.environ.get("JSON",""))
except Exception: sys.exit(0)
for t in d.get("active",[]) or []:
    if t.get("owner_agent")==agent and t.get("status")=="active":
        print(t.get("title","")); break
PY
)"

python3 - "$CONTEXT" "$TITLE" <<'PY'
import sys, json
ctx, title = sys.argv[1], sys.argv[2]
out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}
if title:
    out["hookSpecificOutput"]["sessionTitle"] = title
print(json.dumps(out))
PY
exit 0
'''

PRE_COMPACT_SH = r'''#!/usr/bin/env bash
# fulcra-coord PreCompact hook — ALWAYS checkpoint the session's task before context loss.
# Fail-safe: any error -> exit 0.
set +e
# Bash ARRAY so a spaced argv[0] survives `"${FULCRA_COORD[@]}"` expansion (C1).
FULCRA_COORD=(__FULCRA_COORD_ARGV__)
INPUT="$(cat 2>/dev/null)"
SID="$(printf '%s' "$INPUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null)"
TP="$(printf '%s' "$INPUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("transcript_path",""))' 2>/dev/null)"
[ -z "$SID" ] && SID="$CLAUDE_CODE_SESSION_ID"
[ -z "$SID" ] && exit 0
TASK="$("${FULCRA_COORD[@]}" __session-task "$SID" 2>/dev/null)"
[ -z "$TASK" ] && exit 0
"${FULCRA_COORD[@]}" update "$TASK" \
  --summary "Context compaction checkpoint ($(date -u +%Y-%m-%dT%H:%M:%SZ)). Transcript: ${TP:-n/a}" \
  >/dev/null 2>&1
exit 0
'''

SESSION_END_SH = r'''#!/usr/bin/env bash
# fulcra-coord SessionEnd hook — park the session's active task as waiting.
# Fail-safe: any error -> exit 0.
set +e
# Bash ARRAY so a spaced argv[0] survives `"${FULCRA_COORD[@]}"` expansion (C1).
FULCRA_COORD=(__FULCRA_COORD_ARGV__)
INPUT="$(cat 2>/dev/null)"
SID="$(printf '%s' "$INPUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null)"
[ -z "$SID" ] && SID="$CLAUDE_CODE_SESSION_ID"
[ -z "$SID" ] && exit 0
TASK="$("${FULCRA_COORD[@]}" __session-task "$SID" 2>/dev/null)"
[ -z "$TASK" ] && exit 0
STATUS="$("${FULCRA_COORD[@]}" status --format json 2>/dev/null | TASK="$TASK" python3 -c 'import sys,json,os
tid=os.environ["TASK"]
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
for t in d.get("active",[]) or []:
    if t.get("id")==tid: print(t.get("status","")); break' 2>/dev/null)"
[ "$STATUS" = "active" ] || exit 0
"${FULCRA_COORD[@]}" pause "$TASK" --next "Session ended; resume from last next_action." >/dev/null 2>&1
exit 0
'''


MANAGED_DIRNAME = "fulcra-coord-hooks"
_SCRIPTS = {
    "session-start.sh": SESSION_START_SH,
    "pre-compact.sh": PRE_COMPACT_SH,
    "session-end.sh": SESSION_END_SH,
}
_EVENT_FOR = {
    "session-start.sh": ("SessionStart", "startup|resume|clear|compact"),
    "pre-compact.sh": ("PreCompact", None),
    "session-end.sh": ("SessionEnd", None),
}


def _settings_path(scope: str) -> Path:
    base = Path.home() / ".claude" if scope == "global" else Path.cwd() / ".claude"
    return base / "settings.json"


def _hooks_dir(scope: str = "global") -> Path:
    # Scope-aware so a --project install is self-contained: the materialized
    # scripts and the settings.json hook commands must point at the SAME tree.
    # A global install lives under the user's HOME; a project install lives
    # under the project's own .claude/ so it travels with the repo and never
    # references the operator's HOME (which a teammate would not have).
    base = Path.home() / ".claude" if scope == "global" else Path.cwd() / ".claude"
    return base / MANAGED_DIRNAME


def _is_managed(cmd: str) -> bool:
    return MANAGED_DIRNAME in cmd


def install_claude_code(*, scope: str = "global", uninstall: bool = False,
                        dry_run: bool = False) -> dict[str, Any]:
    settings_path = _settings_path(scope)
    hooks_dir = _hooks_dir(scope)
    plan: dict[str, Any] = {"settings": str(settings_path), "hooks_dir": str(hooks_dir),
                            "uninstall": uninstall, "scripts": [], "events": []}

    settings: Any = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text())
        except ValueError:
            # Unparseable JSON: back up the user's original bytes before we
            # overwrite, so their content is never silently destroyed. The
            # backup is best-effort — failing it must not abort the install.
            if not dry_run:
                try:
                    bak = settings_path.with_suffix(settings_path.suffix + ".bak")
                    bak.write_bytes(settings_path.read_bytes())
                except OSError:
                    pass
            settings = {}
    # Structurally-malformed-but-valid JSON (e.g. a top-level list) would break
    # the dict assumptions below; coerce anything non-dict to an empty config.
    if not isinstance(settings, dict):
        settings = {}

    existing_hooks = settings.get("hooks")
    if not isinstance(existing_hooks, dict):
        # hooks may legally be absent, but a non-dict (list, scalar) is junk we
        # cannot merge into — start clean rather than crashing on .get/.setdefault.
        existing_hooks = {}
        settings["hooks"] = existing_hooks
    hooks = settings.setdefault("hooks", {}) if not dry_run else dict(existing_hooks)

    # Always strip our managed entries first (idempotent + clean uninstall).
    for event, _matcher in _EVENT_FOR.values():
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            # A malformed event value (dict/scalar instead of a list of entries)
            # carries nothing we can keep; drop it and rebuild cleanly.
            entries = []
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue  # skip non-dict junk entries
            entry_hooks = [h for h in entry.get("hooks", []) if not _is_managed(h.get("command", ""))]
            if entry_hooks:
                entry = {**entry, "hooks": entry_hooks}
                kept.append(entry)
            elif not entry.get("hooks"):
                kept.append(entry)
        if kept:
            hooks[event] = kept
        elif event in hooks:
            del hooks[event]

    if not uninstall:
        for fname, (event, matcher) in _EVENT_FOR.items():
            cmd = str(hooks_dir / fname)
            plan["scripts"].append(cmd)
            plan["events"].append(event)
            entry: dict[str, Any] = {"hooks": [{"type": "command", "command": cmd}]}
            if matcher:
                entry["matcher"] = matcher
            hooks.setdefault(event, []).append(entry)

    if dry_run:
        # `hooks` is a working copy in dry-run mode (settings["hooks"] was not
        # reassigned), so reflect the merged result explicitly — otherwise the
        # printed "would write" omits the entries we are about to add.
        plan["would_write"] = {**settings, "hooks": hooks}
        return plan

    if not uninstall:
        # Gap 1: resolve a concretely-callable CLI invocation NOW and bake it
        # into the materialized scripts, so the hooks work under uv-tool /
        # source installs where a bare `fulcra-coord` is not on PATH. The
        # committed template + parity copies keep the literal placeholder.
        from .cli_invocation import (
            resolve_cli_argv, resolve_cli_command, materialize_argv, PLACEHOLDER_ARGV,
        )
        argv = resolve_cli_argv()
        # Display string for the plan (shell-quoted); the scripts get the array.
        plan["resolved_cli"] = resolve_cli_command()
        substituted = materialize_argv(argv)
        hooks_dir.mkdir(parents=True, exist_ok=True)
        for fname, body in _SCRIPTS.items():
            p = hooks_dir / fname
            p.write_text(body.replace(PLACEHOLDER_ARGV, substituted))
            p.chmod(0o755)

    settings["hooks"] = hooks
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return plan

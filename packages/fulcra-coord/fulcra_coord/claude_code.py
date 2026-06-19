"""Claude Code auto-integration: hook-script templates + installer.

Hook-script contents are the single source of truth here (shipped in the wheel).
`install_claude_code` materializes them to ~/.claude/fulcra-coord-hooks/ and wires
settings.json. Committed copies under adapters/claude-code/hooks/ are kept in sync
by a parity test for repo readability.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from . import wake
from .cli_invocation import PLACEHOLDER_ARGV  # re-exported for adapter/test use

CONNECT_FLAGS_PLACEHOLDER = "__FULCRA_COORD_CONNECT_FLAGS__"
CONNECT_FLAGS_FILENAME = "fulcra-coord-connect-flags.json"

SESSION_START_SH = r'''#!/usr/bin/env bash
# fulcra-coord SessionStart hook — surface in-flight + possibly-forgotten work.
# Fail-safe: any error -> exit 0, inject nothing, never block the session.
set +e
INPUT="$(cat 2>/dev/null)"
CWD="$(printf '%s' "$INPUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("cwd",""))' 2>/dev/null)"
[ -z "$CWD" ] && CWD="$PWD"
HOST="$(hostname -s 2>/dev/null || echo host)"
# macOS `hostname -s` returns the transient "Mac" when the system HostName is
# unset, which would mint a phantom claude-code:Mac:<dir> identity that diverges
# from the Python resolver (identity._stable_hostname). When HOST is generic,
# fall back to the stable scutil names so this fail-safe id stays in agreement
# with resolve_agent's derivation. (Only fires for a CLI too old to emit
# `.agent` in `briefing`; the normal path uses the resolved id above.)
case "$(printf '%s' "$HOST" | tr 'A-Z' 'a-z')" in
  ''|mac|localhost|local|localdomain|host)
    for __fc_k in LocalHostName ComputerName; do
      __fc_n="$(scutil --get "$__fc_k" 2>/dev/null | sed -e 's/\..*$//' -e 's/[^A-Za-z0-9-][^A-Za-z0-9-]*/-/g' -e 's/^-*//' -e 's/-*$//')"
      case "$(printf '%s' "$__fc_n" | tr 'A-Z' 'a-z')" in
        ''|mac|localhost|local|localdomain|host) ;;
        *) HOST="$__fc_n"; break ;;
      esac
    done ;;
esac
REPO="$(basename "$CWD")"
STALE_HOURS="${FULCRA_COORD_STALE_HOURS:-2}"
# Resolved at install time (Gap 1) so the hook works under uv-tool / source
# installs where a bare `fulcra-coord` is not on PATH. A bash ARRAY (not a
# string) so a resolved argv[0] containing a space (e.g. an interpreter under
# "~/Library/Application Support/") survives intact under `"${FULCRA_COORD[@]}"`
# expansion — an unquoted string would word-split it into broken tokens (C1).
FULCRA_COORD=(__FULCRA_COORD_ARGV__)

# ONE foreground CLI process for identity + status + inbox + needs-me: the
# `briefing` subcommand folds all four surfaces from a SINGLE summaries load.
# (PERF: the old four-process shape paid 4 CLI spawns + 4 independent
# views/summaries.json downloads per session start — and under the stale-view
# guard each process could re-run the whole direct-listing fallback, so a
# degraded bus cost up to 4 repair-shaped bursts. One process = one load = at
# most ONE fallback.)
# Deliberately NO --agent: passing it is highest-precedence in resolve_agent and
# would OVERRIDE a persisted (`identity set`) or $FULCRA_COORD_AGENT identity,
# so directives addressed to a declared id would be missed (I1). Deliberately NO
# --human either: the CLI resolves the operator's handle from
# $FULCRA_COORD_HUMAN / persisted config / the 'human' default — never hardcode
# a name here. Fail-safe: a missing/old CLI without `briefing` yields empty ->
# inject nothing, never block the session.
BRIEFING="$("${FULCRA_COORD[@]}" briefing --format json 2>/dev/null)"
[ -z "$BRIEFING" ] && exit 0

# The briefing carries the CLI-resolved agent id so EVERY section agrees on
# "who am I" (I-2) — the same identity.resolve_agent resolution inbox/needs-me
# fold with, so the banner's "mine" filter, title, and resume hint can never
# diverge from a declared (`identity set`) id. Fail-safe: an id-less payload
# falls back to the shell-derived id (the same shape resolve_agent derives),
# so the hook still works pre-handshake.
AGENT="$(printf '%s' "$BRIEFING" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("agent",""))' 2>/dev/null)"
[ -z "$AGENT" ] && AGENT="claude-code:${HOST}:${REPO}"

# Report presence on connect (situational awareness): record this agent's current
# workstream(s) on the bus so `agents`/`presence` show what it's working on even
# when it owns no active task. `connect` auto-derives workstreams from this
# agent's open tasks. Backgrounded + silenced — best-effort, never blocks or
# delays session start; a missing/old CLI without `connect` simply no-ops.
CONNECT_FLAGS=(__FULCRA_COORD_CONNECT_FLAGS__)
"${FULCRA_COORD[@]}" connect "${CONNECT_FLAGS[@]}" >/dev/null 2>&1 &

CONTEXT="$(BRIEFING="$BRIEFING" AGENT="$AGENT" STALE_HOURS="$STALE_HOURS" FULCRA_COORD="${FULCRA_COORD[*]}" python3 - <<'PY' 2>/dev/null
import sys, json, os, datetime, shlex
agent = os.environ.get("AGENT","")
stale_h = float(os.environ.get("STALE_HOURS","2"))
try:
    b = json.loads(os.environ.get("BRIEFING","")) or {}
    d = b.get("status") or {}
except Exception:
    sys.exit(0)
# Inbox / needs-me are optional add-on sections; a briefing payload missing
# either key (a leaner future CLI) must not break the in-flight+stale section,
# so each defaults empty — the same fail-safe contract the old per-command
# calls had.
inbox = (b.get("inbox") or {}).get("inbox", []) or []
_nm = b.get("needs_me") or {}
needsme = _nm.get("items", []) or []
# Upcoming = future-not_before asks the human cannot act on yet. They must
# NEVER inflate the BLOCKED ON YOU headline count (the whole point of the
# not_before gate); they only add one muted "+N upcoming" line. An old CLI
# omits the key -> empty, so the banner degrades to the prior shape.
upcoming = _nm.get("upcoming", []) or []
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

# Title = first of my active tasks, if any (read off the briefing's status
# section — same data the old standalone `status` call carried).
TITLE="$(BRIEFING="$BRIEFING" AGENT="$AGENT" python3 - <<'PY' 2>/dev/null
import sys,json,os
agent=os.environ.get("AGENT","")
try: d=(json.loads(os.environ.get("BRIEFING","")) or {}).get("status") or {}
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
# Continuity park (best-effort, BACKGROUNDED so it can never delay compaction;
# runs BEFORE the session-task early-exits because a session can hold a ROLE
# with no coord task). If this session holds role(s) and the optional
# fulcra-continuity CLI is installed, checkpoint each held role and point its
# registry checkpoint_ref at the published snapshot. `park` itself never
# exits nonzero; missing continuity / no roles is a silent no-op.
"${FULCRA_COORD[@]}" park >/dev/null 2>&1 &
[ -z "$SID" ] && SID="$CLAUDE_CODE_SESSION_ID"
[ -z "$SID" ] && exit 0
TASK="$("${FULCRA_COORD[@]}" __session-task "$SID" 2>/dev/null)"
[ -z "$TASK" ] && exit 0
"${FULCRA_COORD[@]}" update "$TASK" \
  --summary "PreCompact continuity checkpoint ($(date -u +%Y-%m-%dT%H:%M:%SZ)). Context is about to be summarized; use resume --with-continuity and inspect transcript ${TP:-n/a}. If decisions, artifacts, or open questions changed since the last update, enrich the task before handoff." \
  >/dev/null 2>&1
"${FULCRA_COORD[@]}" snapshot "$TASK" \
  --reason pre-compact \
  --transcript-path "${TP:-}" \
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
TP="$(printf '%s' "$INPUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("transcript_path",""))' 2>/dev/null)"
# Continuity park (best-effort, BACKGROUNDED so it can never block session
# exit; BEFORE the session-task early-exits because a session can hold a ROLE
# with no coord task). Checkpoints each held role via the optional
# fulcra-continuity CLI and updates the role's checkpoint_ref — the resume
# point the next claimer of the role gets. Never exits nonzero.
"${FULCRA_COORD[@]}" park >/dev/null 2>&1 &
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
"${FULCRA_COORD[@]}" update "$TASK" \
  --summary "SessionEnd continuity checkpoint ($(date -u +%Y-%m-%dT%H:%M:%SZ)). Session is ending; use resume --with-continuity. Transcript: ${TP:-n/a}. If this checkpoint is thin, enrich the task before handoff." \
  >/dev/null 2>&1
"${FULCRA_COORD[@]}" pause "$TASK" --next "Session ended; use resume --with-continuity, then continue from the task next_action and latest continuity checkpoint." --snapshot >/dev/null 2>&1
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


def _connect_flags_path(base_dir: Path) -> Path:
    return base_dir / CONNECT_FLAGS_FILENAME


def _is_managed(cmd: str) -> bool:
    return MANAGED_DIRNAME in cmd


def _normalize_roles(roles: "list[str] | None") -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for role in roles or []:
        role = (role or "").strip()
        if role and role not in seen:
            seen.add(role)
            out.append(role)
    return out


def _load_connect_flags(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _effective_connect_flags(
    path: Path, *, can_review: bool = False, roles: "list[str] | None" = None,
    persist: bool = False, dry_run: bool = False,
) -> tuple[bool, list[str]]:
    saved = _load_connect_flags(path)
    saved_can_review = bool(saved.get("can_review"))
    saved_roles = _normalize_roles(saved.get("roles") if isinstance(saved.get("roles"), list) else None)
    effective_can_review = saved_can_review or bool(can_review)
    effective_roles = _normalize_roles(roles) if roles is not None else saved_roles
    if persist and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "can_review": effective_can_review,
            "roles": effective_roles,
        }, indent=2) + "\n")
    return effective_can_review, effective_roles


def materialize_connect_flags(*, can_review: bool = False,
                              roles: "list[str] | None" = None) -> str:
    flags: list[str] = []
    if can_review:
        flags.append("--can-review")
    for role in roles or []:
        role = (role or "").strip()
        if role:
            flags.extend(["--role", role])
    return " ".join(shlex.quote(flag) for flag in flags)


def materialize_script(body: str, argv_body: str, *,
                       can_review: bool = False,
                       roles: "list[str] | None" = None) -> str:
    return (
        body.replace(PLACEHOLDER_ARGV, argv_body)
        .replace(CONNECT_FLAGS_PLACEHOLDER,
                 materialize_connect_flags(can_review=can_review, roles=roles))
    )


def install_claude_code(*, scope: str = "global", uninstall: bool = False,
                        dry_run: bool = False, can_review: bool = False,
                        roles: "list[str] | None" = None) -> dict[str, Any]:
    settings_path = _settings_path(scope)
    hooks_dir = _hooks_dir(scope)
    flags_path = _connect_flags_path(settings_path.parent)
    plan: dict[str, Any] = {"settings": str(settings_path), "hooks_dir": str(hooks_dir),
                            "connect_flags_file": str(flags_path),
                            "uninstall": uninstall, "scripts": [], "events": []}
    effective_can_review, effective_roles = _effective_connect_flags(
        flags_path, can_review=can_review, roles=roles,
        persist=(not uninstall and (can_review or roles is not None)),
        dry_run=dry_run)
    plan["connect_flags"] = materialize_connect_flags(
        can_review=effective_can_review, roles=effective_roles)

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
        # PLACEHOLDER_ARGV is deliberately NOT re-imported here — the
        # module-level import (re-exported for adapter/test use) already
        # binds it; a local import would just shadow that name.
        from .cli_invocation import (
            resolve_cli_argv, resolve_cli_command, materialize_argv,
        )
        argv = resolve_cli_argv()
        # Display string for the plan (shell-quoted); the scripts get the array.
        plan["resolved_cli"] = resolve_cli_command()
        substituted = materialize_argv(argv)
        hooks_dir.mkdir(parents=True, exist_ok=True)
        for fname, body in _SCRIPTS.items():
            p = hooks_dir / fname
            p.write_text(materialize_script(
                body, substituted, can_review=effective_can_review,
                roles=effective_roles))
            p.chmod(0o755)
    else:
        try:
            flags_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    settings["hooks"] = hooks
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return plan


# ---------------------------------------------------------------------------
# Host wake-exec adapter (--with-wake).
#
# The core wake mechanism (fulcra_coord.wake) is platform-neutral by pinned
# invariant — it must never know what it spawns. So the ONLY place a concrete
# Claude Code command may live is per-adopter config, and this is the installer
# that seeds it: a documented-placeholder entry in
# ${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/wake.json that the OPERATOR is
# expected to review (the spawned session runs with the host's default
# permissions). The default argv is deliberately small — a `claude -p` run with
# a self-contained prompt — because the config file IS the customization point:
# binary path, permission flags, timeouts, model choice all belong in the
# operator's wake.json edit, not in more installer flags.
# ---------------------------------------------------------------------------

def _default_wake_prompt(agent: str) -> str:
    """The documented placeholder prompt for a headless wake session: name the
    agent identity (so the fresh session resolves the right inbox), point it at
    the standing rules, demand evidence-closed loops, and END — a wake session
    is disposable by design; continuity lives on the bus, not in the session."""
    return (f"BUS WAKE: you are {agent}. Use the fulcra-coord CLI as the bus "
            "source of truth: run `fulcra-coord inbox --agent "
            f"{agent}` and `fulcra-coord resume --agent {agent}`. Do not look "
            "for a local tasks/ directory. Act only on directives/verdicts for "
            "this agent, close loops with evidence, then exit.")


def default_wake_entry(agent: str) -> dict[str, Any]:
    """The seed wake.json entry for ``agent``. Conservative defaults: at most
    one wake per 15 min, 900s advisory runtime budget, enabled (the operator
    just asked for it via --with-wake; the loud review note covers consent)."""
    return {
        # --dangerously-skip-permissions is the DELIBERATE default (operator
        # decision 2026-06-10): a woken session that stalls on permission
        # prompts is a smart notifier, not a worker — the point of host-wake is
        # acting unattended. Risk is bounded: it only processes bus directives,
        # on the operator's own machines, under AGENTS.md rules. Soften per
        # host by editing this entry in wake.json (the customization point).
        "cmd": ["claude", "-p", "--dangerously-skip-permissions",
                _default_wake_prompt(agent)],
        # Host schedulers usually run from HOME or /; a woken Claude session
        # needs the worktree that installed this entry so AGENTS.md, MCP/plugin
        # config, and local repo tools are in scope.
        "cwd": str(Path.cwd()),
        "min_interval_min": 15,
        "max_runtime_s": 900,
        "enabled": True,
    }


def install_wake(agent: str, *, uninstall: bool = False,
                 dry_run: bool = False) -> dict[str, Any]:
    """Merge (or remove) ``agent``'s wake entry in wake.json.

    Merge semantics — the file is shared per-adopter policy, so surgery only:

      * other agents' entries are NEVER touched;
      * an existing entry for THIS agent is PRESERVED, not overwritten — the
        config file is the operator's customization point and a reinstall must
        not undo their tuned cmd/interval (``plan["preserved"]`` reports it);
      * uninstall pops only this agent's key;
      * dry-run computes the resulting file but writes nothing;
      * an unparseable existing file is backed up to ``wake.json.bak`` before
        being replaced (mirrors the settings.json merge) so operator content
        is never silently destroyed.

    The path comes from ``wake._wake_config_path()`` — the SAME helper the
    runtime loader uses, so installer and mechanism can never disagree on
    where config lives.
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
        # Reinstall over an operator-tuned entry: keep theirs.
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
            pass  # the backup is best-effort; the merge still proceeds
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return plan

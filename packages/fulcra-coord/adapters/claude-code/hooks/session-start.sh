#!/usr/bin/env bash
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

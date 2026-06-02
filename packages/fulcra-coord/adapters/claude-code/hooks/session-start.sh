#!/usr/bin/env bash
# fulcra-coord SessionStart hook — surface in-flight + possibly-forgotten work.
# Fail-safe: any error -> exit 0, inject nothing, never block the session.
set +e
INPUT="$(cat 2>/dev/null)"
CWD="$(printf '%s' "$INPUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("cwd",""))' 2>/dev/null)"
[ -z "$CWD" ] && CWD="$PWD"
HOST="$(hostname -s 2>/dev/null || echo host)"
REPO="$(basename "$CWD")"
AGENT="claude-code:${HOST}:${REPO}"
STALE_HOURS="${FULCRA_COORD_STALE_HOURS:-2}"
# Resolved at install time (Gap 1) so the hook works under uv-tool / source
# installs where a bare `fulcra-coord` is not on PATH. A bash ARRAY (not a
# string) so a resolved argv[0] containing a space (e.g. an interpreter under
# "~/Library/Application Support/") survives intact under `"${FULCRA_COORD[@]}"`
# expansion — an unquoted string would word-split it into broken tokens (C1).
FULCRA_COORD=(__FULCRA_COORD_ARGV__)

JSON="$("${FULCRA_COORD[@]}" status --format json 2>/dev/null)"
[ -z "$JSON" ] && exit 0

# Directives addressed to this agent. status JSON may not carry `assignee`, so
# we ask the inbox command directly (fail-safe: empty/missing -> no section).
# This is the only extra call; it is silent and never blocks the session.
# Deliberately NO --agent: passing it is highest-precedence in resolve_agent and
# would OVERRIDE a persisted (`identity set`) or $FULCRA_COORD_AGENT identity,
# so directives addressed to a declared id would be missed. Letting inbox resolve
# its own agent honors the declared identity and falls back to the same derived
# "claude-code:${HOST}:${REPO}" id when none is set (I1).
INBOX="$("${FULCRA_COORD[@]}" inbox --format json 2>/dev/null)"

CONTEXT="$(JSON="$JSON" INBOX="$INBOX" AGENT="$AGENT" STALE_HOURS="$STALE_HOURS" FULCRA_COORD="${FULCRA_COORD[*]}" python3 - <<'PY' 2>/dev/null
import sys, json, os, datetime
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
if not mine and not stale and not inbox:
    sys.exit(0)
lines = ["Fulcra coordination — open work on the shared bus:"]
for t in mine:
    lines.append(f"  [{t.get('status','?').upper()}] {t['id']} — {t.get('title','')}")
    if t.get("next_action"):
        lines.append(f"      next: {t['next_action']}")
seen = {t['id'] for t in mine}
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
lines.append("  To resume: "+os.environ.get("FULCRA_COORD","fulcra-coord")+" update <id> --status active --agent "+agent)
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

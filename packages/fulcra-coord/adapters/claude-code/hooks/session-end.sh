#!/usr/bin/env bash
# fulcra-coord SessionEnd hook — park the session's active task as waiting.
# Fail-safe: any error -> exit 0.
set +e
# Bash ARRAY so a spaced argv[0] survives `"${FULCRA_COORD[@]}"` expansion (C1).
FULCRA_COORD=(__FULCRA_COORD_ARGV__)
INPUT="$(cat 2>/dev/null)"
SID="$(printf '%s' "$INPUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null)"
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
"${FULCRA_COORD[@]}" pause "$TASK" --next "Session ended; resume from last next_action." --snapshot >/dev/null 2>&1
exit 0

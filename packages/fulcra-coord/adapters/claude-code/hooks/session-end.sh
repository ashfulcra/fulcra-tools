#!/usr/bin/env bash
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

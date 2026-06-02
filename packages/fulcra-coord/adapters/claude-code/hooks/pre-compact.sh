#!/usr/bin/env bash
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

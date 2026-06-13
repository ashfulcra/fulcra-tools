#!/usr/bin/env bash
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

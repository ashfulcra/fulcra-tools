#!/bin/bash
# coord2 SessionStart hook — resume brief + inbox count. Output is bounded:
# the legacy hook's unbounded board dump is a known context-flooding failure.
# Degrades silently (exit 0) if coord-engine is not on PATH.
set +e
TEAM="__TEAM__"; AGENT="__AGENT__"
export FULCRA_COORD_AGENT="$AGENT"
command -v coord-engine >/dev/null 2>&1 || exit 0
BRIEF="$(coord-engine continuity resume "$TEAM" "$AGENT" 2>/dev/null | head -25)"
INBOX="$(coord-engine inbox "$TEAM" --agent "$AGENT" 2>/dev/null | head -8)"
[ -z "$BRIEF$INBOX" ] && exit 0
python3 - "$BRIEF" "$INBOX" <<'EOF'
import json, sys
brief, inbox = sys.argv[1], sys.argv[2]
ctx = "coord2 resume brief:\n" + brief + "\n\ncoord2 inbox:\n" + inbox
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart", "additionalContext": ctx[:4000]}}))
EOF
exit 0

#!/bin/bash
# coord SessionStart hook — resume brief + briefing (THE entry fold: identity,
# role inboxes, needs-me incl pending reviews). Output is bounded: the legacy
# hook's unbounded board dump is a known context-flooding failure — each source
# is head-capped and the whole context is clamped to 4000 chars.
# Degrades silently (exit 0) if coord-engine is not on PATH.
set +e
# __TEAM__/__AGENT__ are rendered by install-claude-code.sh as shell-quoted
# literals (shlex.quote), so a bare assignment here round-trips any id verbatim.
TEAM=__TEAM__; AGENT=__AGENT__
export FULCRA_COORD_AGENT="$AGENT"
command -v coord-engine >/dev/null 2>&1 || exit 0
BRIEF="$(coord-engine continuity resume "$TEAM" "$AGENT" 2>/dev/null | head -25)"
BRIEFING="$(coord-engine briefing "$TEAM" --agent "$AGENT" 2>/dev/null | head -60)"
[ -z "$BRIEF$BRIEFING" ] && exit 0
python3 - "$BRIEF" "$BRIEFING" <<'EOF'
import json, sys
brief, briefing = sys.argv[1], sys.argv[2]
ctx = "coord resume brief:\n" + brief + "\n\ncoord briefing:\n" + briefing
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart", "additionalContext": ctx[:4000]}}))
EOF
exit 0

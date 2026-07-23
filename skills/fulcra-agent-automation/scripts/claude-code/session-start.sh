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
WAKE_CONTEXT="$(coord-engine wake consume "$TEAM" --agent "$AGENT" 2>/dev/null | head -8)"
BRIEF="$(coord-engine continuity resume "$TEAM" "$AGENT" 2>/dev/null | head -25)"
BRIEFING="$(coord-engine briefing "$TEAM" --agent "$AGENT" 2>/dev/null | head -60)"
[ -z "$WAKE_CONTEXT$BRIEF$BRIEFING" ] && exit 0
python3 - "$WAKE_CONTEXT" "$BRIEF" "$BRIEFING" <<'EOF'
import json, sys
wake, brief, briefing = sys.argv[1], sys.argv[2], sys.argv[3]
parts = []
if wake:
    parts.append("coord wake nudge:\n" + wake)
if brief:
    parts.append("coord resume brief:\n" + brief)
if briefing:
    parts.append("coord briefing:\n" + briefing)
ctx = "\n\n".join(parts)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart", "additionalContext": ctx[:4000]}}))
EOF
exit 0

#!/bin/bash
# coord2 park-on-context-loss hook (SessionEnd).
# Backgrounded, never blocks; degrades silently if coord-engine is absent.
set +e
# __TEAM__/__AGENT__ are rendered by install-claude-code.sh as shell-quoted
# literals (shlex.quote), so a bare assignment here round-trips any id verbatim.
TEAM=__TEAM__; AGENT=__AGENT__
export FULCRA_COORD_AGENT="$AGENT"
command -v coord-engine >/dev/null 2>&1 || exit 0
coord-engine continuity park "$TEAM" --agent "$AGENT" \
  --objective "context-loss park ($(date -u +%Y-%m-%dT%H:%MZ))" >/dev/null 2>&1 &
exit 0

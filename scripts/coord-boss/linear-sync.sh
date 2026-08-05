#!/bin/bash
set -u
SS="/tmp/claude-0/-home-user-fulcra-tools/a07b97e8-9d5f-59f3-8df6-9ceba3d40af6/scratchpad"
LOG="$SS/linear-sync.log"
export PATH="$HOME/.local/bin:$PATH"
# BUS-74: environment config is the secret source of truth. When the injected
# vars are present, (re)materialize the 0600 env files the bridge tools read —
# so a fresh container needs no secret handling at all. File fallback remains
# for containers predating the env config.
if [ -n "${LINEAR_API_KEY:-}" ]; then
  umask 077
  printf 'LINEAR_API_KEY="%s"\nLINEAR_TEAM_KEY=%s\nLINEAR_TEAM_ID=%s\n' \
    "$LINEAR_API_KEY" "${LINEAR_TEAM_KEY:-BUS}" "${LINEAR_TEAM_ID:?}" > "$SS/linear.env"
  printf 'LINEAR_API_KEY=%s\nLINEAR_TEAM_KEY=%s\nLINEAR_TEAM_ID=%s\n' \
    "$LINEAR_API_KEY" "${LINEAR_TEAM_KEY:-BUS}" "${LINEAR_TEAM_ID:?}" > "$SS/answers-linear.env"
fi
[ -f "$SS/linear.env" ] || { echo "BLOCKED: no LINEAR_API_KEY env var and no linear.env on disk"; exit 3; }
set -a; . "$SS/linear.env"; set +a
cd /home/user/fulcra-tools || exit 1
git fetch origin main >/dev/null 2>&1
WT="$SS/wt-cutover"
if [ ! -d "$WT" ]; then git worktree add "$WT" origin/main >/dev/null 2>&1; fi
git -C "$WT" fetch origin main >/dev/null 2>&1 && git -C "$WT" reset --hard origin/main >/dev/null 2>&1
cd "$WT" || exit 1
TS=$(date -u +%FT%TZ)
SYNC=$(uv run --project packages/coord-tracker-bridge coord-tracker-bridge sync --coord-team fulcra --source engine --principal coord-boss 2>&1)
RC=$?
APPLIED=$(printf '%s' "$SYNC" | python3 -c "import json,sys
raw=sys.stdin.read()
try:
    i=raw.index('{\"applied\"'); d=json.JSONDecoder().raw_decode(raw[i:])[0]
    print(d.get('applied','?'))
except Exception: print('ERR')" 2>/dev/null)
PROMOTE=$(ANSWERS_LINEAR_ENV="$SS/answers-linear.env" python3 /home/user/fulcra-tools/tools/answers-bridge/answers_bridge.py promote 2>&1 | tail -1)
PRC=$?
echo "[$TS] sync rc=$RC applied=$APPLIED | promote rc=$PRC: $PROMOTE" >> "$LOG"
echo "sync rc=$RC applied=$APPLIED | promote rc=$PRC: $PROMOTE"
[ "$RC" -eq 0 ] && [ "$PRC" -eq 0 ] && exit 0 || exit 2

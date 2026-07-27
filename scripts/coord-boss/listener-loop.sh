#!/bin/bash
SP=/tmp/claude-0/-home-user-fulcra-tools/a07b97e8-9d5f-59f3-8df6-9ceba3d40af6/scratchpad
PIDF=$SP/coord-boss-listener.pid
if [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null && [ "$(cat $PIDF)" != "$$" ]; then exit 0; fi
echo $$ > "$PIDF"
DEG=0; LASTBEAT=0
END=$(( $(date +%s) + 604800 ))
while [ "$(date +%s)" -lt "$END" ]; do
  NOW=$(date +%s)
  if [ $((NOW - LASTBEAT)) -ge 1800 ]; then
    coord-engine presence beat fulcra --agent coord-boss >/dev/null 2>&1
    coord-engine roles claim fulcra coord-boss --agent coord-boss >/dev/null 2>&1
    : # escalate DISARMED until presence-liveness fix ships (activity-without-beats reads as dead)
    LASTBEAT=$NOW
  fi
  OUT=$(coord-engine listen fulcra --agent coord-boss --once 2> >(grep -v 'owner unresolved for' >&2))
  RC=$?
  if [ $RC -ne 0 ] || echo "$OUT" | grep -q 'LISTEN DEGRADED'; then
    DEG=$((DEG+1)); [ $DEG -ge 2 ] && exit 2
  else
    DEG=0
  fi
  if [ -n "$OUT" ]; then echo "$OUT"; exit 0; fi
  sleep 60
done

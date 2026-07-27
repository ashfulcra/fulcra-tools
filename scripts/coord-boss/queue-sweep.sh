#!/bin/bash
# Bus v3 queue sweep — ONE bounded typed-record read, run once per wake.
#
# This replaced bus-sweep.sh (which walked /team/fulcra/task/ — the retired
# file-tree discovery path) and listener-loop.sh (a resident poll loop —
# retired by the bus v3 operator contract, 2026-07-27). It is NOT a loop and
# must never be wrapped in one: schedule it (cron/Routine/heartbeat) or run it
# on wake. Quiet output means no events in the window, not proof of nothing —
# a transport error prints DEGRADED and exits 3 (fail closed).
#
# The filter program rides in a variable and is passed via -c: it must NOT be
# fed to `python3 -` over a heredoc while the data is piped, because both
# claim stdin and the data silently loses (shipped that bug 2026-07-27; a
# 34-record window printed zero events while exiting 0).
#
# USAGE: queue-sweep.sh [AGENT] [WINDOW]     defaults: coord-boss, "70 minutes"
set -u
AGENT="${1:-coord-boss}"
WINDOW="${2:-70 minutes}"
COORD_TYPE="MomentAnnotation/ea49d0d3-acb7-49c6-93b6-bee81d126c92"

FILTER='
import json, sys
# One malformed record LINE means the window is UNKNOWN: print nothing, say
# DEGRADED, exit 3 — same contract as FulcraFileTransport.records(). A record
# whose *note* is not a v1 payload is different: that is an ordinary
# annotation sharing the track, and skipping it is correct.
agent = sys.argv[1]
seen = set()
out = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        rec = json.loads(line)
    except ValueError:
        print("DEGRADED: malformed record line — window UNKNOWN, not empty",
              file=sys.stderr)
        sys.exit(3)
    if not isinstance(rec, dict):
        print("DEGRADED: non-object record line — window UNKNOWN, not empty",
              file=sys.stderr)
        sys.exit(3)
    if rec.get("id") in seen:
        continue
    seen.add(rec.get("id"))
    try:
        p = json.loads(rec.get("note") or "")
    except ValueError:
        continue
    if not isinstance(p, dict) or p.get("v") != 1:
        continue
    if p.get("to") not in (agent, "all"):
        continue
    src = [s for s in rec.get("sources", []) if not s.startswith("com.fulcradynamics.")]
    out.append(" ".join(str(x) for x in (
        rec.get("recorded_at", "")[:19], src[0] if src else "?",
        p.get("kind"), p.get("pri"), p.get("slug"), p.get("ptr") or "-")))
for row in out:
    print(row)
'

OUT="$(timeout 90 fulcra-api get-records "$COORD_TYPE" "$WINDOW" 2>/dev/null)"
rc=$?
if [ $rc -ne 0 ]; then
  echo "DEGRADED: get-records rc=$rc — window UNKNOWN, not empty" >&2
  exit 3
fi

printf '%s\n' "$OUT" | python3 -c "$FILTER" "$AGENT"

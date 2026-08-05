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
# USAGE: queue-sweep.sh [AGENT]     defaults: coord-boss
#
# This wrapper deliberately does NOT commit a cursor-v2 delivery. The agent
# must process every JSONL event and only then invoke:
#   coord-engine queue commit fulcra --agent "$AGENT" --token "$TOKEN" \
#     --result "$RECORD_ID=$OUTCOME"   # repeat for every staged event
# A wrapper that commits immediately after printing recreates print-before-
# process loss.
set -u
AGENT="${1:-coord-boss}"

coord-engine queue fulcra --agent "$AGENT" --json
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "QUEUE READ FAILED (rc=$rc): coord-engine queue failed; a failing read proves nothing about the queue" >&2
fi
exit "$rc"

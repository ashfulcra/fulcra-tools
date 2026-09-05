#!/usr/bin/env bash
# A stand-in for `coord-tracker-bridge`, driven by FAKE_SCRIPT.
#
# FAKE_SCRIPT is a comma-separated list of responses, consumed one per
# invocation, so a test can say "this plan, then this sync, then this plan".
# Each entry names a fixture in scripts/tests/fixtures/ or a bare word:
#   fail        -> exit non-zero (a read that proves nothing)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${FAKE_STATE:?FAKE_STATE must point at a counter file}"
N=$(( $(cat "$STATE" 2>/dev/null || echo 0) ))
echo $((N + 1)) > "$STATE"
IFS=',' read -r -a STEPS <<< "${FAKE_SCRIPT:?FAKE_SCRIPT must be set}"
STEP="${STEPS[$N]:-${STEPS[-1]}}"
if [ "$STEP" = "fail" ]; then
  echo "fake bridge: simulated read failure" >&2
  exit 1
fi
cat "$HERE/fixtures/$STEP.json"

#!/usr/bin/env bash
# Scheduled projection of the coord bus into Linear.
#
# WHY THE GUARDRAILS ARE HERE AND NOT IN A PROMPT. A cadence means this runs
# with nobody reading the output. Every refusal below is a failure this lane
# actually had while a person WAS watching and caught it; unattended, the same
# failure writes itself onto the operator's board and stays there.
#
# Exit codes follow the bridge's own contract:
#   0  converged, nothing owed
#   2  a deliberate refusal -- something needs a human before it is safe
#   3  UNKNOWN -- the run proves nothing. Never rendered as "nothing to do".
set -uo pipefail

: "${LINEAR_API_KEY:?LINEAR_API_KEY must be set to the BOT token, never a personal key}"
: "${LINEAR_TEAM_ID:?LINEAR_TEAM_ID must be set}"

REPO="${REPO:-/home/user/fulcra-tools}"
SOURCE="${SOURCE:-engine}"
# A close is the only irreversible-looking change here. A handful is ordinary
# churn; a pile means the source read is wrong, and the absence-close gate has
# already been wrong once in this package's history. Refuse and let a human look.
MAX_CLOSES="${MAX_CLOSES:-8}"
BRIDGE=(uv run --project "$REPO/packages/coord-tracker-bridge" python -m coord_tracker_bridge.cli)
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '%s\n' "$*"; }

plan_into() {  # $1 = output path
  if ! "${BRIDGE[@]}" plan --source "$SOURCE" > "$1" 2>"$WORK/err"; then
    say "UNKNOWN: plan failed. This proves nothing about the board."
    sed -n 1,20p "$WORK/err"
    return 1
  fi
}

summarize() {  # $1 = plan json; sets CHANGES, CLOSES, RESOURCES
  read -r CHANGES CLOSES RESOURCES < <(python3 - "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
changes = d["changes"]
closes = sum(1 for c in changes if c["kind"] == "close")
res = len(d["resources"]["labels"]) + len(d["resources"]["projects"])
print(len(changes), closes, res)
PY
) || return 1
}

# A SOURCE THAT MOVES IS NOT A SYNC THAT FAILED.
#
# The durability check compares the plan before and after. Unattended, on a bus
# a live fleet writes to continuously, the second plan is routinely non-empty
# just because rows changed in the intervening minutes -- and calling that a
# failure every time is crying wolf until nobody reads the alarm.
#
# The bug it must still catch is specific and different: a row this run just
# WROTE still asking to be written, forever. So compare identities, not counts.
# A row carried over from the first plan is the failure; a row that is new since
# it is the world moving on, and the next run takes it.
unsettled_carryover() {  # $1 = plan before, $2 = plan after -> count on stdout
  python3 "$REPO/scripts/_carryover.py" "$1" "$2"
}

plan_into "$WORK/plan.json" || exit 3
summarize "$WORK/plan.json" || exit 3

if [ "$RESOURCES" -gt 0 ]; then
  # sync refuses this itself, but saying so plainly beats a stack trace: a label
  # or project has to be created before first use, and the bot token is
  # routinely forbidden from creating labels.
  say "REFUSED: $RESOURCES resource(s) must be created first. Run apply-resources deliberately."
  exit 2
fi

if [ "$CHANGES" -eq 0 ]; then
  say "converged: 0 changes."
  exit 0
fi

if [ "$CLOSES" -gt "$MAX_CLOSES" ]; then
  say "REFUSED: $CLOSES closes exceeds MAX_CLOSES=$MAX_CLOSES (of $CHANGES changes)."
  say "A pile of closes means the source read is wrong, not that the work vanished."
  say "Nothing was applied. Inspect the plan before raising the cap."
  exit 2
fi

say "applying $CHANGES change(s), $CLOSES close(s)"
if ! "${BRIDGE[@]}" sync --source "$SOURCE" > "$WORK/sync.json" 2>"$WORK/err"; then
  say "UNKNOWN: sync did not report success. Some changes may have landed."
  sed -n 1,20p "$WORK/err"
  exit 3
fi
APPLIED="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["applied"])' "$WORK/sync.json")" || exit 3

# rc=0 IS ACCEPTANCE, NOT DURABILITY. The only proof a sync settled is a second
# plan returning zero. A sync once reported applied:3 and the next plan proposed
# the same three updates forever.
plan_into "$WORK/verify.json" || { say "applied $APPLIED but convergence is UNVERIFIED"; exit 3; }
summarize "$WORK/verify.json" || exit 3
REMAINING="$CHANGES"
CARRIED="$(unsettled_carryover "$WORK/plan.json" "$WORK/verify.json" 2>"$WORK/carried")" || exit 3
if [ "$CARRIED" -gt 0 ]; then
  say "applied $APPLIED, but $CARRIED row(s) this run WROTE are still proposed. NOT settled:"
  cat "$WORK/carried"
  exit 3
fi
if [ "$REMAINING" -gt 0 ]; then
  # New work that appeared while this run was in flight. The next run takes it.
  say "applied $APPLIED, settled. $REMAINING new change(s) arrived during the run; next run picks them up."
  exit 0
fi
say "applied $APPLIED, converged: second plan returns 0 changes."

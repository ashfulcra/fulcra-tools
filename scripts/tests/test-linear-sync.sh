#!/usr/bin/env bash
# Drives linear-sync.sh through every refusal it can make.
#
# WHY THIS EXISTS: those refusals are the entire reason the script exists, they
# only ever run when nobody is watching, and until now not one of them had ever
# executed -- they had been read and assumed correct. A healthy board cannot be
# asked to propose a mass close on demand, so the bridge is driven by a fake
# through a real command seam.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../linear-sync.sh"
PASS=0; FAIL=0

check() {  # name, expected_exit, expected_substring, FAKE_SCRIPT
  local name="$1" want_rc="$2" want_text="$3" script="$4"
  local state out rc
  state="$(mktemp)"; echo 0 > "$state"
  out="$(LINEAR_API_KEY=fake LINEAR_TEAM_ID=fake \
        BRIDGE_BIN="$HERE/fake-bridge.sh" FAKE_SCRIPT="$script" FAKE_STATE="$state" \
        bash "$SCRIPT" 2>&1)"
  rc=$?
  rm -f "$state"
  if [ "$rc" -ne "$want_rc" ]; then
    printf 'FAIL %s: exit %s, wanted %s\n     output: %s\n' "$name" "$rc" "$want_rc" "$out"
    FAIL=$((FAIL+1)); return
  fi
  if ! printf '%s' "$out" | grep -qF "$want_text"; then
    printf 'FAIL %s: output missing %q\n     output: %s\n' "$name" "$want_text" "$out"
    FAIL=$((FAIL+1)); return
  fi
  printf 'ok   %s (exit %s)\n' "$name" "$rc"
  PASS=$((PASS+1))
}

# 0 -- nothing to do is success, and must not run a sync.
check "converged board does nothing"        0 "converged: 0 changes"     "converged"

# 2 -- a label or project must exist before first use, and the bot token is
# routinely forbidden from creating one. Refuse rather than fail mid-write.
check "resource plan refuses"               2 "REFUSED: 1 resource"      "needs-resources"

# 2 -- a pile of closes means the source read is wrong, not that work vanished.
# This package once queued 52 live cards for closing on exactly that mistake.
check "mass close refuses"                  2 "REFUSED: 12 closes"       "mass-close"

# 3 -- a failed read proves nothing. Never "nothing to do".
check "unreadable plan is UNKNOWN"          3 "UNKNOWN: plan failed"     "fail"

# 3 -- rc=0 is acceptance, not durability: the SAME rows still proposed after
# the sync is the "applied 3, proposes the same 3 forever" bug.
check "carried-over rows fail the run"      3 "still proposed"           "two-updates,sync-ok,same-two"

# 0 -- DIFFERENT rows are the fleet writing while we ran. Calling that a failure
# every run is crying wolf until nobody reads the alarm.
check "new rows since the plan pass"        0 "arrived during the run"   "two-updates,sync-ok,other-two"

# 0 -- the ordinary happy path.
check "applied and converged"               0 "converged: second plan"   "two-updates,sync-ok,converged"

# 3 -- the sync itself failing means some changes may have landed.
check "sync failure is UNKNOWN"             3 "UNKNOWN: sync"            "two-updates,fail"

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

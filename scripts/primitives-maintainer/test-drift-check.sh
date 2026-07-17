#!/bin/bash
# Regression tests for drift-check.sh.
#
# Every case runs the real script against a scratch PRIMITIVES_STATE_DIR, a stub
# coord-engine that captures the tell instead of posting it, file:// probe URLs,
# and a stub CLI. Nothing here touches the live baseline, the bus, or PyPI.
#
# The theme: a probe result is a real observation or UNKNOWN, never a default
# that type-checks as data. Most of these tests exist because a failure was
# quietly being written as a legal-looking value.
#
#   ./test-drift-check.sh          run all
#   ./test-drift-check.sh -v       show each run's alert/tell output
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="$HERE/drift-check.sh"
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS=0; FAIL=0

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

ok()   { PASS=$((PASS+1)); green "  ok: $*"; }
bad()  { FAIL=$((FAIL+1)); red   "  FAIL: $*"; }

# --- fixtures ---------------------------------------------------------------

# A click-shaped CLI. Env knobs break specific sub-probes so we can prove a
# broken sub-probe cannot become a value.
#   FAKE_VERBS         top-level verb list (default: the 0.1.38 surface, no record/delete)
#   FAKE_FAIL_GROUP    this group's --help exits 1 (probe dies)
#   FAKE_FAIL_BETA     --beta --help exits 1 (probe dies)
#   FAKE_NO_BETA       --beta --help exits 2 "No such option" (real observation)
#   FAKE_BETA_VERBS    extra verbs visible only under --beta
#   FAKE_LEAF_GROUP    this group's --help succeeds with NO Commands: block
cat > "$WORK/fake-cli" <<'FAKE'
#!/bin/bash
verbs="${FAKE_VERBS:-auth catalog data-type tag user-info}"
beta=0
[ "${1:-}" = "--beta" ] && { beta=1; shift; }

emit_top() {
  echo "Usage: fulcra-api [OPTIONS] COMMAND [ARGS]..."
  echo
  echo "Options:"
  echo "  --beta  Enable beta features"
  echo "  --help  Show this message and exit."
  echo
  echo "Commands:"
  for v in $verbs; do echo "  $v  Some description"; done
  if [ "$beta" = "1" ]; then
    for v in ${FAKE_BETA_VERBS:-}; do echo "  $v  Beta description"; done
  fi
}

# `--beta --help` with no subcommand
if [ "$beta" = "1" ] && [ "${1:-}" = "--help" ]; then
  if [ -n "${FAKE_NO_BETA:-}" ]; then
    echo "Usage: fulcra-api [OPTIONS] COMMAND [ARGS]..." >&2
    echo "Try 'fulcra-api --help' for help." >&2
    echo >&2
    echo "Error: No such option '--beta'." >&2
    exit 2
  fi
  [ -n "${FAKE_FAIL_BETA:-}" ] && { echo "boom: beta probe died" >&2; exit 1; }
  emit_top
  exit 0
fi

# `--help` (top level)
if [ "${1:-}" = "--help" ]; then emit_top; exit 0; fi

cmd="${1:-}"
if [ "$cmd" = "${FAKE_FAIL_GROUP:-__none__}" ]; then
  echo "boom: group probe died" >&2
  exit 1
fi
if [ "$cmd" = "${FAKE_LEAF_GROUP:-__none__}" ]; then
  echo "Usage: fulcra-api $cmd [OPTIONS]"
  echo
  echo "  Help that parses cleanly but exposes no subcommand block at all."
  exit 0
fi

# Groups with subcommands; anything else is a leaf.
case "$cmd" in
  auth)      subs="login print-access-token" ;;
  data-type) subs="create list schema" ;;
  tag)       subs="create list" ;;
  file)      subs="get put" ;;
  share)     subs="create list" ;;
  *)
    echo "Usage: fulcra-api $cmd [OPTIONS]"
    echo
    echo "  A leaf command."
    exit 0 ;;
esac
echo "Usage: fulcra-api $cmd [OPTIONS] COMMAND [ARGS]..."
echo
echo "Commands:"
for s in $subs; do echo "  $s  Some description"; done
exit 0
FAKE
chmod +x "$WORK/fake-cli"

# Stub coord-engine. Answers the three verbs the scripts use, so the alert path
# is exercised end-to-end without a bus:
#   tell           -> append to $TELL_LOG instead of posting  (FAKE_TELL_RC to fail it)
#   roles status   -> FAKE_ROLE_STATUS (default HELD); "UNKNOWN" exits 1 like the
#                     real engine does when the lease listing is unreadable
#   presence show  -> a roster. Two INDEPENDENT axes, because a role and its
#                     holder are different things and the whole point of the
#                     resolver is that a lease on one says nothing about the other:
#                       FAKE_HOLDER_LIVENESS -> the role's fresh holder,
#                                               `stub-holder` (default live)
#                       FAKE_LIVENESS        -> the configured target itself, only
#                                               listed when kind=agent (default live)
#                     "ABSENT" omits that agent from the roster entirely.
#
# Note what the roster is keyed on: agent IDENTITIES. The real engine's
# `fresh_holders` are agent ids and presence shards are per-agent — a ROLE NAME
# never appears in a roster. An earlier stub listed the role name as though it
# were an agent, which only type-checked because the code under test was itself
# addressing the role. Fixtures that model the bug cannot catch it.
cat > "$WORK/fake-coord-engine" <<'CE'
#!/bin/bash
add_agent() {
  [ -n "$entries" ] && entries="$entries,"
  entries="$entries{\"agent\":\"$1\",\"liveness\":\"$2\",\"last_seen\":\"2026-07-16T21:00:00Z\"}"
}
case "$1 ${2:-}" in
  "roles status")
    st="${FAKE_ROLE_STATUS:-HELD}"
    if [ "$st" = "UNKNOWN" ]; then
      echo "lease state unknown for role $4 in team/$3 — degraded transport, retry" >&2
      exit 1
    fi
    if [ "$st" = "HELD" ]; then
      printf '{"team":"%s","role":"%s","status":"HELD","fresh_holders":["stub-holder"],"holders":["stub-holder"]}\n' "$3" "$4"
    else
      printf '{"team":"%s","role":"%s","status":"%s","fresh_holders":[],"holders":[]}\n' "$3" "$4" "$st"
    fi
    exit 0 ;;
  "presence show")
    entries=""
    [ "${FAKE_HOLDER_LIVENESS:-live}" = "ABSENT" ] \
      || add_agent "stub-holder" "${FAKE_HOLDER_LIVENESS:-live}"
    if [ "${PRIMITIVES_TARGET_KIND:-role}" = "agent" ] && [ "${FAKE_LIVENESS:-live}" != "ABSENT" ]; then
      add_agent "${PRIMITIVES_TARGET:-fulcra-primitives-maintainer}" "${FAKE_LIVENESS:-live}"
    fi
    printf '[%s]\n' "$entries"
    exit 0 ;;
  "tell "*)
    printf '%s\n' "$*" >> "${TELL_LOG:-/dev/null}"
    if [ -n "${FAKE_TELL_RC:-}" ] && [ "${FAKE_TELL_RC}" != "0" ]; then
      echo "directive failed: task already exists" >&2
      exit "$FAKE_TELL_RC"
    fi
    exit 0 ;;
esac
echo "fake-coord-engine: unhandled: $*" >&2
exit 64
CE
chmod +x "$WORK/fake-coord-engine"

printf '{"info": {"version": "0.1.38"}}' > "$WORK/pypi.json"
printf '{"scopes_supported": ["openid", "profile", "name", "email"]}' > "$WORK/mcp.json"

# A minimal but valid OpenAPI doc containing the sentinel path.
cat > "$WORK/openapi.json" <<'SPEC'
{"paths": {"/user/v1alpha1/annotation": {"get": {}, "post": {}},
           "/user/v1alpha1/catalog": {"get": {}}},
 "components": {"schemas": {"DataRecordV1": {}}}}
SPEC

# Valid JSON, valid OpenAPI shape, wrong document: no sentinel path.
cat > "$WORK/openapi-wrong.json" <<'SPEC'
{"paths": {"/pets": {"get": {}}, "/pets/{id}": {"get": {}, "delete": {}}},
 "components": {"schemas": {"Pet": {}}}}
SPEC

# --- runner -----------------------------------------------------------------
# run <state-dir> [VAR=VAL ...] -> sets RC, and the state dir holds the markers.
run() {
  local state="$1"; shift
  mkdir -p "$state"
  local out
  out="$(env \
    PRIMITIVES_STATE_DIR="$state" \
    PRIMITIVES_COORD_ENGINE="$WORK/fake-coord-engine" \
    TELL_LOG="$state/tells.txt" \
    PRIMITIVES_PYPI_URL="file://$WORK/pypi.json" \
    PRIMITIVES_MCP_URL="file://$WORK/mcp.json" \
    PRIMITIVES_SPEC_URL="file://$WORK/openapi.json" \
    PRIMITIVES_CLI_CMD="$WORK/fake-cli" \
    PRIMITIVES_SKIP_GH=1 \
    "$@" \
    /bin/bash "$SUT" 2>&1)"
  RC=$?
  [ "$VERBOSE" = "1" ] && { echo "--- stdout/stderr ---"; echo "$out"; }
  return 0
}

# -e/-- so a pattern starting with a dash (--from …) is a pattern, not a flag.
has() { grep -q -e "$2" -- "$1" 2>/dev/null; }

# ============================================================================
echo
echo "1. group --help fails -> UNKNOWN, exit 2, NO baseline"
# The old code omitted the group from the fingerprint: a group whose probe dies
# permanently read as 'that group has no subcommands' forever.
S="$WORK/t1"; run "$S" FAKE_FAIL_GROUP=auth
[ "$RC" = "2" ] && ok "exit 2" || bad "expected exit 2, got $RC"
[ ! -f "$S/baseline.json" ] && ok "no baseline written" || bad "baseline was written: $(cat "$S/baseline.json")"
has "$S/PROBE-UNKNOWN.txt" "auth: --help exited 1" && ok "UNKNOWN names the dead group probe" \
  || bad "UNKNOWN marker missing the group failure: $(cat "$S/PROBE-UNKNOWN.txt" 2>/dev/null)"
has "$S/tells.txt" "UNKNOWN" && ok "alerted" || bad "no tell"

echo
echo "2. --beta --help fails -> UNKNOWN, exit 2, NO baseline"
# The nastiest case: the healthy beta surface is legitimately empty, so the old
# `or []` produced byte-for-byte the value a healthy run produces.
S="$WORK/t2"; run "$S" FAKE_FAIL_BETA=1
[ "$RC" = "2" ] && ok "exit 2" || bad "expected exit 2, got $RC"
[ ! -f "$S/baseline.json" ] && ok "no baseline written" || bad "baseline was written: $(cat "$S/baseline.json")"
has "$S/PROBE-UNKNOWN.txt" "beta: --help exited 1" && ok "UNKNOWN names the dead beta probe" \
  || bad "UNKNOWN marker missing the beta failure: $(cat "$S/PROBE-UNKNOWN.txt" 2>/dev/null)"

echo
echo "3. failed beta probe never compares clean against a healthy empty beta"
# Baseline from a healthy run (beta legitimately []), then the beta probe dies.
# Under the old `or []` this was exit 0 'no drift'.
S="$WORK/t3"; run "$S"
[ "$RC" = "0" ] && ok "healthy first run baselines" || bad "expected 0, got $RC"
grep -q '"cli_beta_verbs": \[\]' "$S/baseline.json" && ok "healthy beta is legitimately []" \
  || bad "expected empty beta list: $(cat "$S/baseline.json")"
run "$S" FAKE_FAIL_BETA=1
[ "$RC" = "2" ] && ok "dead beta probe = exit 2, not 'no drift'" || bad "expected 2, got $RC"
grep -q '"cli_beta_verbs": \[\]' "$S/baseline.json" && ok "baseline unchanged" || bad "baseline moved"

echo
echo "4. sentinel group returns no Commands: block -> positive control fails"
# The probe 'succeeded' at every step; the shape is what is wrong.
S="$WORK/t4"; run "$S" FAKE_LEAF_GROUP=auth
[ "$RC" = "2" ] && ok "exit 2" || bad "expected exit 2, got $RC"
[ ! -f "$S/baseline.json" ] && ok "no baseline written" || bad "baseline was written"
has "$S/PROBE-UNKNOWN.txt" "sentinel group auth" && ok "control names the sentinel group" \
  || bad "control did not fire: $(cat "$S/PROBE-UNKNOWN.txt" 2>/dev/null)"

echo
echo "5. a build with no --beta flag is an observation, not UNKNOWN or []"
S="$WORK/t5"; run "$S" FAKE_NO_BETA=1
[ "$RC" = "0" ] && ok "exit 0" || bad "expected exit 0, got $RC"
grep -q '"cli_beta_verbs": "NO_BETA_FLAG"' "$S/baseline.json" \
  && ok "recorded NO_BETA_FLAG, distinct from []" || bad "got: $(cat "$S/baseline.json")"

echo
echo "6. 0.1.37 simulation: record/delete land -> drift fires and NAMES record"
S="$WORK/t6"; run "$S"
[ "$RC" = "0" ] && ok "baseline written (pre-0.1.37 surface)" || bad "expected 0, got $RC"
run "$S" FAKE_VERBS="auth catalog data-type tag user-info record delete"
[ "$RC" = "1" ] && ok "exit 1 (drift)" || bad "expected exit 1, got $RC"
has "$S/DRIFT-ALERT.txt" "cli_verbs: ADDED delete, record" && ok "alert names record" \
  || bad "alert does not name record: $(cat "$S/DRIFT-ALERT.txt" 2>/dev/null)"
has "$S/DRIFT-ALERT.txt" "FULL-REWRITE TRIGGER" && ok "flagged as the full-rewrite trigger" || bad "trigger not flagged"
has "$S/tells.txt" "P1" && ok "tell is P1" || bad "tell not P1: $(cat "$S/tells.txt")"
has "$S/tells.txt" "not data_types.py" && ok "tell routes away from data_types.py" || bad "no routing note"

echo
echo "7. unreachable spec URL -> UNKNOWN, exit 2, NO baseline"
S="$WORK/t7"; run "$S" PRIMITIVES_SPEC_URL="file://$WORK/nope-does-not-exist.json"
[ "$RC" = "2" ] && ok "exit 2" || bad "expected exit 2, got $RC"
[ ! -f "$S/baseline.json" ] && ok "no baseline written" || bad "baseline was written"
has "$S/PROBE-UNKNOWN.txt" "spec_hash" && ok "UNKNOWN names spec_hash" || bad "spec_hash not named"

echo
echo "8. valid-but-wrong OpenAPI doc -> sentinel trips"
S="$WORK/t8"; run "$S" PRIMITIVES_SPEC_URL="file://$WORK/openapi-wrong.json"
[ "$RC" = "2" ] && ok "exit 2" || bad "expected exit 2, got $RC"
[ ! -f "$S/baseline.json" ] && ok "no baseline written" || bad "baseline was written"
has "$S/PROBE-UNKNOWN.txt" "sentinel path /user/v1alpha1/annotation absent" \
  && ok "spec sentinel named" || bad "sentinel not named: $(cat "$S/PROBE-UNKNOWN.txt" 2>/dev/null)"

echo
echo "9. drift -> UNKNOWN -> healthy: the original WHAT CHANGED survives"
# The reviewed defect: the UNKNOWN branch truncated DRIFT-ALERT.txt, so the
# rewrite that was owed vanished once the probes recovered.
S="$WORK/t9"; run "$S"
[ "$RC" = "0" ] && ok "baseline written" || bad "expected 0, got $RC"
run "$S" FAKE_VERBS="auth catalog data-type tag user-info record delete"
[ "$RC" = "1" ] && ok "drift detected, baseline advanced" || bad "expected 1, got $RC"
ORIG="$(cat "$S/DRIFT-ALERT.txt")"
run "$S" FAKE_FAIL_GROUP=auth
[ "$RC" = "2" ] && ok "UNKNOWN run" || bad "expected 2, got $RC"
[ "$(cat "$S/DRIFT-ALERT.txt")" = "$ORIG" ] && ok "UNKNOWN did not touch DRIFT-ALERT.txt" \
  || bad "UNKNOWN rewrote the drift alert"
has "$S/PROBE-UNKNOWN.txt" "is ALSO outstanding" && ok "UNKNOWN marker points at the open drift debt" \
  || bad "UNKNOWN marker does not mention the outstanding drift"
run "$S" FAKE_FAIL_GROUP=tag   # arbitrary number of UNKNOWN runs
run "$S" FAKE_FAIL_BETA=1
[ "$(cat "$S/DRIFT-ALERT.txt")" = "$ORIG" ] && ok "survives repeated UNKNOWN runs" || bad "drift alert lost"
run "$S" FAKE_VERBS="auth catalog data-type tag user-info record delete"   # probes recover
[ "$RC" = "1" ] && ok "healthy run exits 1 (outstanding), not 0" || bad "expected 1, got $RC"
[ "$(cat "$S/DRIFT-ALERT.txt")" = "$ORIG" ] && ok "original WHAT CHANGED still intact after recovery" \
  || bad "drift alert changed"
has "$S/DRIFT-ALERT.txt" "cli_verbs: ADDED delete, record" && ok "still actionable: names record" || bad "payload lost"
[ ! -f "$S/PROBE-UNKNOWN.txt" ] && ok "recovered run cleared the probe marker only" || bad "PROBE-UNKNOWN.txt not cleared"
grep -q "OUTSTANDING" "$S/tells.txt" && ok "re-alerted OUTSTANDING" || bad "no OUTSTANDING tell"

echo
echo "10. a second drift appends, it does not overwrite the first"
S="$WORK/t10"; run "$S"
run "$S" FAKE_VERBS="auth catalog data-type tag user-info record"
[ "$RC" = "1" ] && ok "first drift" || bad "expected 1, got $RC"
run "$S" FAKE_VERBS="auth catalog data-type tag user-info record delete"
[ "$RC" = "1" ] && ok "second drift" || bad "expected 1, got $RC"
has "$S/DRIFT-ALERT.txt" "cli_verbs: ADDED record$" && ok "first drift payload retained" \
  || bad "first drift lost: $(cat "$S/DRIFT-ALERT.txt")"
has "$S/DRIFT-ALERT.txt" "cli_verbs: ADDED delete$" && ok "second drift payload present" || bad "second drift missing"
has "$S/DRIFT-ALERT.txt" "ADDITIONAL DRIFT" && ok "separator marks the unactioned pile-up" || bad "no separator"

echo
echo "11. steady state: no drift, no alert -> exit 0"
S="$WORK/t11"; run "$S"
run "$S"
[ "$RC" = "0" ] && ok "exit 0" || bad "expected 0, got $RC"
[ ! -f "$S/DRIFT-ALERT.txt" ] && ok "no alert" || bad "alert written on a clean run"

echo
echo "12. group subcommand drift is seen (groups are discovered, not hardcoded)"
S="$WORK/t12"; run "$S"
run "$S" FAKE_VERBS="auth catalog data-type tag user-info share"
[ "$RC" = "1" ] && ok "exit 1" || bad "expected 1, got $RC"
has "$S/DRIFT-ALERT.txt" "cli_groups\[share\]: ADDED create, list" && ok "new group's subcommands named" \
  || bad "group drift not described: $(cat "$S/DRIFT-ALERT.txt")"

echo
echo "13. MCP discovery doc without the sentinel scope -> UNKNOWN"
printf '{"scopes_supported": ["something-else"]}' > "$WORK/mcp-wrong.json"
S="$WORK/t13"; run "$S" PRIMITIVES_MCP_URL="file://$WORK/mcp-wrong.json"
[ "$RC" = "2" ] && ok "exit 2" || bad "expected 2, got $RC"
has "$S/PROBE-UNKNOWN.txt" "sentinel scope openid absent" && ok "mcp sentinel named" || bad "mcp sentinel did not fire"

# ============================================================================
# The alert path. Everything above proves the detector sees the change; these
# prove somebody hears about it. On 2026-07-16 the first half worked and the
# second half did not, and the run looked identical to a clean one.

echo
echo "14. the tell is addressed to the ROLE'S RESOLVED HOLDER, sent by the resolved identity"
# This test used to assert the opposite — `tell fulcra fulcra-primitives-maintainer`,
# the ROLE NAME — and it was wrong in the way the whole file is about. Addressing a
# role reaches only a live `listen`: coord_engine's cmd_inbox, cmd_briefing and
# query.needs_me all fold without held_roles, so a lease-holder running an ordinary
# heartbeat never sees it and `tell` still returns 0. The role stays the CONFIGURED
# target (sessions stop; roles outlive them) but is resolved to a holder identity at
# send time, which every consumer path folds.
S="$WORK/t14"; run "$S" FULCRA_COORD_AGENT=claude-code:test-host:primitives
run "$S" FULCRA_COORD_AGENT=claude-code:test-host:primitives \
  FAKE_VERBS="auth catalog data-type tag user-info record"
has "$S/tells.txt" "tell fulcra stub-holder" && ok "addressed to the role's resolved holder" \
  || bad "not addressed to the holder identity: $(cat "$S/tells.txt")"
! grep -q "tell fulcra fulcra-primitives-maintainer" "$S/tells.txt" \
  && ok "never addressed to the bare role name" \
  || bad "addressed to the ROLE NAME — only a live \`listen\` folds that: $(cat "$S/tells.txt")"
has "$S/tells.txt" "--from claude-code:test-host:primitives" && ok "sender resolved from the env" \
  || bad "sender not resolved: $(cat "$S/tells.txt")"
! grep -q "claude-code:Mac:" "$S/tells.txt" && ok "no baked-in host identity" \
  || bad "the hardcoded Mac identity is back: $(cat "$S/tells.txt")"

echo
echo "15. no shipped script assigns a hardcoded agent identity"
# The actual 2026-07-16 defect, as a grep: one host's session id, assigned to a
# variable, in a script that ships to every host. Matches an ASSIGNMENT of a
# literal identity, so the prose explaining the bug does not trip it. Covers the
# weekly too — the same line was in both, which is what a copy-pasted alert path
# gets you.
# The files are named, so a rename must fail the test rather than silently
# grepping nothing and reporting clean — an assertion whose subject vanished is
# not an assertion that passed.
SHIPPED="$HERE/drift-check.sh $HERE/weekly-review.sh $HERE/lib-alert.sh"
MISSING=""
for f in $SHIPPED; do [ -f "$f" ] || MISSING="$MISSING $f"; done
HARDCODED="$(grep -nE '^[^#]*[A-Za-z_]+=("|'"'"')?(claude-code|codex|openclaw|workbook):' \
  $SHIPPED 2>/dev/null || true)"
if [ -n "$MISSING" ]; then
  bad "cannot check for hardcoded identities — file(s) missing:$MISSING"
elif [ -n "$HARDCODED" ]; then
  bad "a host/session identity is hardcoded: $HARDCODED"
else
  ok "identity is resolved at runtime in every shipped script"
fi

echo
echo "16. clean run + role nobody holds -> exit 3, marker, NOT a silent 0"
# The whole point: a clean run whose alert path is dead must not read as "fine".
S="$WORK/t16"; run "$S"                       # baseline with a healthy path
[ "$RC" = "0" ] && ok "healthy path -> exit 0" || bad "expected 0, got $RC"
[ ! -f "$S/ALERT-UNDELIVERED.txt" ] && ok "no marker on a healthy path" || bad "marker written spuriously"
run "$S" FAKE_ROLE_STATUS=VACANT
[ "$RC" = "3" ] && ok "exit 3, not 0" || bad "expected 3, got $RC"
has "$S/ALERT-UNDELIVERED.txt" "role fulcra-primitives-maintainer is VACANT" \
  && ok "marker names the vacant role" || bad "marker missing: $(cat "$S/ALERT-UNDELIVERED.txt" 2>/dev/null)"

echo
echo "17. unverifiable target (degraded transport) is a problem, not a pass"
S="$WORK/t17"; run "$S" FAKE_ROLE_STATUS=UNKNOWN
[ "$RC" = "3" ] && ok "exit 3" || bad "expected 3, got $RC"
has "$S/ALERT-UNDELIVERED.txt" "cannot verify target" && ok "marker says it could not check" \
  || bad "marker did not name the failed check: $(cat "$S/ALERT-UNDELIVERED.txt" 2>/dev/null)"

echo
echo "18. drift + dead alert path: exit stays 1, and the marker carries the drift"
# 3 must not swallow 1 — the drift is the more specific summons.
S="$WORK/t18"; run "$S"
run "$S" FAKE_ROLE_STATUS=VACANT FAKE_VERBS="auth catalog data-type tag user-info record delete"
[ "$RC" = "1" ] && ok "exit 1 (drift outranks the delivery failure)" || bad "expected 1, got $RC"
has "$S/DRIFT-ALERT.txt" "cli_verbs: ADDED delete, record" && ok "drift alert still written" || bad "drift alert lost"
has "$S/ALERT-UNDELIVERED.txt" "WHAT THIS RUN WAS TRYING TO SAY" && ok "marker carries the undelivered payload" \
  || bad "marker has no payload: $(cat "$S/ALERT-UNDELIVERED.txt" 2>/dev/null)"
has "$S/ALERT-UNDELIVERED.txt" "FULL-REWRITE TRIGGER" && ok "the undelivered P1 is legible in the marker" \
  || bad "marker does not carry the trigger"

echo
echo "19. a dropped tell (slug collision, rc 1) is a delivery failure"
# `tell` rc was logged to a file nothing reads: a dropped alert and a delivered
# one produced the same run.
S="$WORK/t19"; run "$S"
run "$S" FAKE_TELL_RC=1 FAKE_VERBS="auth catalog data-type tag user-info record"
has "$S/ALERT-UNDELIVERED.txt" "tell FAILED rc=1" && ok "marker names the dropped tell" \
  || bad "dropped tell not surfaced: $(cat "$S/ALERT-UNDELIVERED.txt" 2>/dev/null)"

echo
echo "20. the path recovering clears the marker (and only it)"
S="$WORK/t20"; run "$S"
run "$S" FAKE_ROLE_STATUS=VACANT
[ -f "$S/ALERT-UNDELIVERED.txt" ] && ok "marker present while the role is vacant" || bad "no marker"
run "$S"
[ "$RC" = "0" ] && ok "exit 0 once the role is held again" || bad "expected 0, got $RC"
[ ! -f "$S/ALERT-UNDELIVERED.txt" ] && ok "marker cleared by the run that could deliver" || bad "marker not cleared"

echo
echo "21. agent-kind target: stale is unreachable, live is reachable"
# The exact 2026-07-16 shape: an identity that exists in presence, last beat 8
# days ago. It is IN the roster — absence was never the tell.
S="$WORK/t21"
run "$S" PRIMITIVES_TARGET_KIND=agent PRIMITIVES_TARGET=claude-code:Mac:fulcra-primitives-maintainer \
  FAKE_LIVENESS=live
[ "$RC" = "0" ] && ok "live named agent -> exit 0" || bad "expected 0, got $RC"
run "$S" PRIMITIVES_TARGET_KIND=agent PRIMITIVES_TARGET=claude-code:Mac:fulcra-primitives-maintainer \
  FAKE_LIVENESS=stale
[ "$RC" = "3" ] && ok "stale named agent -> exit 3" || bad "expected 3, got $RC"
has "$S/ALERT-UNDELIVERED.txt" "liveness=stale" && ok "marker names the liveness" \
  || bad "marker does not name liveness: $(cat "$S/ALERT-UNDELIVERED.txt" 2>/dev/null)"
run "$S" PRIMITIVES_TARGET_KIND=agent PRIMITIVES_TARGET=nobody-here FAKE_LIVENESS=ABSENT
[ "$RC" = "3" ] && ok "target absent from the roster -> exit 3" || bad "expected 3, got $RC"
has "$S/ALERT-UNDELIVERED.txt" "has no presence shard" && ok "marker distinguishes absent from stale" \
  || bad "absent not named: $(cat "$S/ALERT-UNDELIVERED.txt" 2>/dev/null)"

echo
echo "22. a fresh role lease is NOT proof the alert reached anyone"
# Test 16 covers a role NOBODY holds. This is the harder one, and the one that was
# shipped: the role IS held, the lease IS fresh — `roles status` says HELD with a
# fresh holder — and the alert still reaches nobody, because the holder is not
# beating and a role-addressed directive is folded ONLY by a live `listen`
# (coord_engine's cmd_inbox / cmd_briefing / query.needs_me all fold without
# held_roles). A lease proves someone holds the role. It proves nothing consumes
# it. Accepting it here would clear the marker on a healthy-looking proxy while
# `tell` returned 0 — the 2026-07-16 failure, rebuilt one level up.
S="$WORK/t22"; run "$S" FAKE_ROLE_STATUS=HELD FAKE_HOLDER_LIVENESS=stale
[ "$RC" = "3" ] && ok "fresh lease + unreachable holder -> exit 3, not a silent 0" \
  || bad "expected 3, got $RC — the lease was accepted as reachability"
has "$S/ALERT-UNDELIVERED.txt" "fresh holder, but no holder is reachable" \
  && ok "marker refuses the lease as evidence and names the trap" \
  || bad "marker does not name it: $(cat "$S/ALERT-UNDELIVERED.txt" 2>/dev/null)"
has "$S/ALERT-UNDELIVERED.txt" "stub-holder liveness=stale" \
  && ok "marker names the holder that was not listening" \
  || bad "holder not named: $(cat "$S/ALERT-UNDELIVERED.txt" 2>/dev/null)"
[ -f "$S/ALERT-UNDELIVERED.txt" ] && ok "marker NOT cleared by a healthy-looking lease" \
  || bad "marker was cleared on a fresh lease alone"
# Drift + a fresh lease whose holder is dark: the drift exit code must survive, and
# the marker must carry the drift (same contract as test 18's dead path).
run "$S" FAKE_ROLE_STATUS=HELD FAKE_HOLDER_LIVENESS=stale \
  FAKE_VERBS="auth catalog data-type tag user-info record"
[ "$RC" = "1" ] && ok "drift + fresh-but-dark holder: exit stays 1, never masked by 3" \
  || bad "expected 1, got $RC"
has "$S/ALERT-UNDELIVERED.txt" "WHAT THIS RUN WAS TRYING TO SAY" \
  && ok "the undelivered drift is written down locally" \
  || bad "marker lost the drift payload"

echo
printf 'passed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1

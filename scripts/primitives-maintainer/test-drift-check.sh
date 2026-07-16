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

# Stub coord-engine: append the tell to $TELL_LOG instead of posting it.
cat > "$WORK/fake-coord-engine" <<'CE'
#!/bin/bash
printf '%s\n' "$*" >> "${TELL_LOG:-/dev/null}"
exit 0
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

has() { grep -q "$2" "$1" 2>/dev/null; }

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

echo
printf 'passed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1

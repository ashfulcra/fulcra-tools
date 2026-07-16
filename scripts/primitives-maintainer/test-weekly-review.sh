#!/bin/bash
# Regression tests for weekly-review.sh.
#
# Same shape as test-drift-check.sh: the real script, a scratch
# PRIMITIVES_STATE_DIR, a stub coord-engine that captures the tell, and file://
# probe URLs. Nothing here touches the live baseline, the bus, or the network.
#
# The theme, again: a probe result is a real observation or UNKNOWN, never a
# default that type-checks as data. Every test below is a value this script used
# to write into its baseline as though it had observed something —
#   the literal string "FAIL"        (every probe's error fallback)
#   sha256("") = e3b0c44298fc1c14    (what a dead docs URL actually produced)
#   a hash of somebody else's API    (nothing checked whose doc it was)
# — after which "no wide drift" meant nothing at all.
#
#   ./test-weekly-review.sh          run all
#   ./test-weekly-review.sh -v       show each run's output
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="$HERE/weekly-review.sh"
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

cat > "$WORK/fake-coord-engine" <<'CE'
#!/bin/bash
case "$1 ${2:-}" in
  "roles status")
    printf '{"team":"%s","role":"%s","status":"HELD","fresh_holders":["stub-holder"]}\n' "$3" "$4"
    exit 0 ;;
  "presence show")
    printf '[{"agent":"%s","liveness":"live","last_seen":"2026-07-16T21:00:00Z"}]\n' \
      "${PRIMITIVES_TARGET:-fulcra-primitives-maintainer}"
    exit 0 ;;
  "tell "*)
    printf '%s\n' "$*" >> "${TELL_LOG:-/dev/null}"; exit 0 ;;
esac
echo "fake-coord-engine: unhandled: $*" >&2
exit 64
CE
chmod +x "$WORK/fake-coord-engine"

# A valid OpenAPI doc containing the sentinel path.
cat > "$WORK/openapi.json" <<'SPEC'
{"paths": {"/user/v1alpha1/annotation": {"get": {}, "post": {}},
           "/user/v1alpha1/catalog": {"get": {}}},
 "components": {"schemas": {"DataRecordV1": {}, "Annotation": {}}}}
SPEC

# Valid JSON, valid OpenAPI shape, wrong document: no sentinel path. Hashes just
# as cleanly as the real one — which is the entire problem with a bare hash.
cat > "$WORK/openapi-wrong.json" <<'SPEC'
{"paths": {"/pets": {"get": {}}, "/pets/{id}": {"get": {}, "delete": {}}},
 "components": {"schemas": {"Pet": {}}}}
SPEC

# An OpenAPI doc with the sentinel path AND an extra endpoint: real wide drift.
cat > "$WORK/openapi-moved.json" <<'SPEC'
{"paths": {"/user/v1alpha1/annotation": {"get": {}, "post": {}},
           "/user/v1alpha1/catalog": {"get": {}},
           "/user/v1alpha1/record": {"post": {}, "delete": {}}},
 "components": {"schemas": {"DataRecordV1": {}, "Annotation": {}}}}
SPEC

printf '{"scopes_supported": ["openid", "profile", "name", "email"]}' > "$WORK/mcp.json"
printf '{"scopes_supported": ["something-else"]}' > "$WORK/mcp-wrong.json"

# Docs pages, shaped like the real ones: a per-page <title>, and a nav block that
# names EVERY page — which is why the page's identity is checked against the
# title and not the body text.
docs_page() { # $1 file, $2 title, $3 prose
  {
    echo "<html><head><title>$2</title><style>.x{color:red}</style></head><body>"
    echo "<nav>Home REST API Reference Fulcra MCP Server</nav>"
    echo "<h1>Fulcra Dynamics</h1><p>$3</p><p>"
    # Padding — a real page is not 40 characters long.
    for i in $(seq 1 40); do printf 'The Fulcra Life API exposes your data to agents. '; done
    echo "</p></body></html>"
  } > "$1"
}
docs_page "$WORK/docs-overview.html" "Fulcra Developer Docs" "Overview of the platform."
docs_page "$WORK/docs-api.html" "REST API Reference - Fulcra Developer Docs" "Annotation endpoints."
docs_page "$WORK/docs-mcp.html" "Fulcra MCP Server - Fulcra Developer Docs" "MCP server setup."
docs_page "$WORK/docs-overview-changed.html" "Fulcra Developer Docs" "Overview, now with records."

# A 404 body. Real HTML, hashes perfectly, contains no Fulcra prose.
cat > "$WORK/docs-404.html" <<'H'
<html><body><h1>404 — Not Found</h1><p>The requested page does not exist.</p></body></html>
H

# The nastiest fixture: a page that strips to NOTHING. The old code hashed the
# empty string and printed e3b0c44298fc1c14 — a stable, real-looking value.
cat > "$WORK/docs-empty.html" <<'H'
<html><head><script>document.write("everything is rendered client side")</script></head><body><div></div></body></html>
H

# --- runner -----------------------------------------------------------------
run() {
  local state="$1"; shift
  mkdir -p "$state"
  local out
  out="$(env \
    PRIMITIVES_STATE_DIR="$state" \
    PRIMITIVES_COORD_ENGINE="$WORK/fake-coord-engine" \
    TELL_LOG="$state/tells.txt" \
    PRIMITIVES_SPEC_URL="file://$WORK/openapi.json" \
    PRIMITIVES_MCP_URL="file://$WORK/mcp.json" \
    PRIMITIVES_DOCS_OVERVIEW_URL="file://$WORK/docs-overview.html" \
    PRIMITIVES_DOCS_API_URL="file://$WORK/docs-api.html" \
    PRIMITIVES_DOCS_MCP_URL="file://$WORK/docs-mcp.html" \
    "$@" \
    /bin/bash "$SUT" 2>&1)"
  RC=$?
  [ "$VERBOSE" = "1" ] && { echo "--- stdout/stderr ---"; echo "$out"; }
  return 0
}

has() { grep -q -e "$2" -- "$1" 2>/dev/null; }

# ============================================================================
echo
echo "1. healthy week -> baseline written, real hashes, no FAIL anywhere"
S="$WORK/t1"; run "$S"
[ "$RC" = "0" ] && ok "exit 0" || bad "expected 0, got $RC"
[ -f "$S/weekly-baseline.json" ] && ok "baseline written" || bad "no baseline"
! has "$S/weekly-baseline.json" "FAIL" && ok "no FAIL in the baseline" \
  || bad "FAIL baselined: $(cat "$S/weekly-baseline.json")"
! has "$S/weekly-baseline.json" "UNKNOWN" && ok "no UNKNOWN in the baseline" \
  || bad "UNKNOWN baselined: $(cat "$S/weekly-baseline.json")"
has "$S/WEEKLY-REVIEW-DUE.txt" "WEEKLY FULL RE-READ DUE" && ok "re-read flag dropped" || bad "no flag"

echo
echo "2. dead docs URL -> UNKNOWN, exit 2, NO baseline"
# The old code: curl fails -> empty stdin -> python hashes "" -> e3b0c44298fc1c14,
# baselined as an observation, and identical every week forever.
S="$WORK/t2"; run "$S" PRIMITIVES_DOCS_API_URL="file://$WORK/nope-does-not-exist.html"
[ "$RC" = "2" ] && ok "exit 2" || bad "expected 2, got $RC"
[ ! -f "$S/weekly-baseline.json" ] && ok "no baseline written" || bad "baseline written: $(cat "$S/weekly-baseline.json")"
has "$S/WEEKLY-PROBE-UNKNOWN.txt" "docs_api: fetch failed" && ok "UNKNOWN names the dead probe" \
  || bad "marker missing: $(cat "$S/WEEKLY-PROBE-UNKNOWN.txt" 2>/dev/null)"
has "$S/tells.txt" "UNKNOWN" && ok "alerted P1" || bad "no tell: $(cat "$S/tells.txt" 2>/dev/null)"
has "$S/WEEKLY-REVIEW-DUE.txt" "UNKNOWN this week" && ok "the flag says the hash covered nothing" \
  || bad "flag does not admit the UNKNOWN: $(cat "$S/WEEKLY-REVIEW-DUE.txt")"

echo
echo "3. a page that strips to no text is UNKNOWN, not sha256(\"\")"
S="$WORK/t3"; run "$S" PRIMITIVES_DOCS_MCP_URL="file://$WORK/docs-empty.html"
[ "$RC" = "2" ] && ok "exit 2" || bad "expected 2, got $RC"
[ ! -f "$S/weekly-baseline.json" ] && ok "no baseline" || bad "baseline written"
has "$S/WEEKLY-PROBE-UNKNOWN.txt" "docs_mcp" && ok "UNKNOWN names docs_mcp" \
  || bad "not named: $(cat "$S/WEEKLY-PROBE-UNKNOWN.txt" 2>/dev/null)"
! has "$S/WEEKLY-PROBE-UNKNOWN.txt" "e3b0c44298fc1c14" && ok "the empty-string hash never appears" \
  || bad "sha256(\"\") is still being produced"

echo
echo "4. valid-but-wrong docs page (404 body) -> sentinel trips"
S="$WORK/t4"; run "$S" PRIMITIVES_DOCS_OVERVIEW_URL="file://$WORK/docs-404.html"
[ "$RC" = "2" ] && ok "exit 2" || bad "expected 2, got $RC"
has "$S/WEEKLY-PROBE-UNKNOWN.txt" "positive control" && ok "positive control named" \
  || bad "control did not fire: $(cat "$S/WEEKLY-PROBE-UNKNOWN.txt" 2>/dev/null)"
[ ! -f "$S/weekly-baseline.json" ] && ok "a 404 never becomes the baseline" || bad "404 baselined"

echo
echo "5. valid-but-wrong OpenAPI doc -> sentinel path trips"
S="$WORK/t5"; run "$S" PRIMITIVES_SPEC_URL="file://$WORK/openapi-wrong.json"
[ "$RC" = "2" ] && ok "exit 2" || bad "expected 2, got $RC"
has "$S/WEEKLY-PROBE-UNKNOWN.txt" "sentinel path /user/v1alpha1/annotation absent" \
  && ok "spec sentinel named" || bad "sentinel not named: $(cat "$S/WEEKLY-PROBE-UNKNOWN.txt" 2>/dev/null)"

echo
echo "6. MCP discovery without the sentinel scope -> UNKNOWN"
S="$WORK/t6"; run "$S" PRIMITIVES_MCP_URL="file://$WORK/mcp-wrong.json"
[ "$RC" = "2" ] && ok "exit 2" || bad "expected 2, got $RC"
has "$S/WEEKLY-PROBE-UNKNOWN.txt" "sentinel scope openid absent" && ok "mcp sentinel named" \
  || bad "mcp sentinel did not fire: $(cat "$S/WEEKLY-PROBE-UNKNOWN.txt" 2>/dev/null)"

echo
echo "7. transient outage: recovery does NOT read as drift"
# The old sequence: outage bakes FAIL into the baseline, then RECOVERY differs
# from FAIL and fires a wide-drift alert for a surface that never changed.
S="$WORK/t7"; run "$S"                                   # healthy baseline
BASE="$(cat "$S/weekly-baseline.json")"
run "$S" PRIMITIVES_DOCS_API_URL="file://$WORK/nope.html"  # outage
[ "$RC" = "2" ] && ok "outage -> exit 2" || bad "expected 2, got $RC"
[ "$(cat "$S/weekly-baseline.json")" = "$BASE" ] && ok "outage did not touch the baseline" \
  || bad "baseline moved during the outage: $(cat "$S/weekly-baseline.json")"
rm -f "$S/WEEKLY-REVIEW-DUE.txt"                         # a session did the re-read
run "$S"                                                 # recovery
[ "$RC" = "0" ] && ok "recovery -> exit 0, no spurious drift" || bad "expected 0, got $RC"
! has "$S/tells.txt" "WIDE-DRIFT" && ok "no wide-drift tell on recovery" \
  || bad "recovery alerted as drift: $(cat "$S/tells.txt")"
[ ! -f "$S/WEEKLY-PROBE-UNKNOWN.txt" ] && ok "recovery cleared the probe marker" || bad "marker not cleared"

echo
echo "8. persistent outage never reports a clean week"
# The old code: FAIL == FAIL -> "no wide drift" -> clean forever. A permanently
# broken probe was indistinguishable from a stable surface.
S="$WORK/t8"
for i in 1 2 3 4; do run "$S" PRIMITIVES_DOCS_MCP_URL="file://$WORK/gone.html"; done
[ "$RC" = "2" ] && ok "still exit 2 on the 4th consecutive dead week" || bad "expected 2, got $RC"
[ ! -f "$S/weekly-baseline.json" ] && ok "never baselined a value it did not observe" \
  || bad "baseline appeared: $(cat "$S/weekly-baseline.json")"
[ "$(grep -c UNKNOWN "$S/tells.txt")" -ge 4 ] && ok "alerted every week, not once" \
  || bad "stopped alerting: $(cat "$S/tells.txt")"

echo
echo "9. real wide drift is still caught, named, and told"
S="$WORK/t9"; run "$S"
run "$S" PRIMITIVES_SPEC_URL="file://$WORK/openapi-moved.json"
[ "$RC" = "1" ] && ok "exit 1 (drift)" || bad "expected 1, got $RC"
has "$S/tells.txt" "WIDE-DRIFT" && ok "wide-drift tell sent" || bad "no tell: $(cat "$S/tells.txt")"
has "$S/tells.txt" "wide_spec" && ok "tell names the field that moved" || bad "field not named"
has "$S/WEEKLY-REVIEW-DUE.txt" "WHAT MOVED" && ok "flag carries what moved" \
  || bad "flag has no payload: $(cat "$S/WEEKLY-REVIEW-DUE.txt")"

echo
echo "10. docs prose drift is caught (the daily cannot see this at all)"
S="$WORK/t10"; run "$S"
run "$S" PRIMITIVES_DOCS_OVERVIEW_URL="file://$WORK/docs-overview-changed.html"
[ "$RC" = "1" ] && ok "exit 1" || bad "expected 1, got $RC"
has "$S/tells.txt" "docs_overview" && ok "tell names docs_overview" || bad "not named: $(cat "$S/tells.txt")"

echo
echo "11. an unactioned re-read is never overwritten"
# Once the baseline has moved past a wide drift, the flag is the only record of
# it. Truncating it each week erased exactly that.
S="$WORK/t11"; run "$S"
run "$S" PRIMITIVES_SPEC_URL="file://$WORK/openapi-moved.json"   # week 1: drift
has "$S/WEEKLY-REVIEW-DUE.txt" "wide_spec" && ok "week 1 payload present" || bad "no week-1 payload"
run "$S" PRIMITIVES_SPEC_URL="file://$WORK/openapi-moved.json"   # week 2: quiet, flag untouched by anyone
[ "$RC" = "1" ] && ok "exit 1 while the re-read is outstanding" || bad "expected 1, got $RC"
has "$S/WEEKLY-REVIEW-DUE.txt" "wide_spec" && ok "week 1 payload SURVIVED week 2" \
  || bad "week 2 truncated the record of week 1: $(cat "$S/WEEKLY-REVIEW-DUE.txt")"
has "$S/WEEKLY-REVIEW-DUE.txt" "STILL OUTSTANDING" && ok "pile-up is marked" || bad "no outstanding marker"
rm -f "$S/WEEKLY-REVIEW-DUE.txt"      # a session does the re-read
run "$S" PRIMITIVES_SPEC_URL="file://$WORK/openapi-moved.json"
[ "$RC" = "0" ] && ok "exit 0 once the re-read is done" || bad "expected 0, got $RC"
! has "$S/WEEKLY-REVIEW-DUE.txt" "STILL OUTSTANDING" && ok "fresh flag after the re-read" || bad "stale text carried over"

echo
echo "12. UNKNOWN does not truncate an outstanding re-read either"
S="$WORK/t12"; run "$S"
run "$S" PRIMITIVES_SPEC_URL="file://$WORK/openapi-moved.json"
ORIG="$(cat "$S/WEEKLY-REVIEW-DUE.txt")"
run "$S" PRIMITIVES_MCP_URL="file://$WORK/gone.json"
[ "$RC" = "2" ] && ok "UNKNOWN week -> exit 2" || bad "expected 2, got $RC"
has "$S/WEEKLY-REVIEW-DUE.txt" "wide_spec" && ok "the earlier payload survives an UNKNOWN week" \
  || bad "UNKNOWN erased it: $(cat "$S/WEEKLY-REVIEW-DUE.txt")"
[ "${#ORIG}" -lt "$(wc -c < "$S/WEEKLY-REVIEW-DUE.txt")" ] && ok "flag grew, was not replaced" || bad "flag shrank"

echo
echo "13. the weekly's UNKNOWN marker is not the daily's"
# They share a state dir. If the weekly wrote PROBE-UNKNOWN.txt, a healthy daily
# run would clear a debt it never checked.
S="$WORK/t13"; run "$S" PRIMITIVES_SPEC_URL="file://$WORK/gone.json"
[ -f "$S/WEEKLY-PROBE-UNKNOWN.txt" ] && ok "weekly writes its own marker" || bad "no weekly marker"
[ ! -f "$S/PROBE-UNKNOWN.txt" ] && ok "does not touch the daily's marker" || bad "clobbered the daily's marker"

echo
echo "14. the alert path is checked here too: vacant role -> exit 3, marker"
S="$WORK/t14"
cat > "$WORK/ce-vacant" <<'CE'
#!/bin/bash
case "$1 ${2:-}" in
  "roles status") printf '{"team":"%s","role":"%s","status":"VACANT","fresh_holders":[]}\n' "$3" "$4"; exit 0 ;;
  "tell "*) printf '%s\n' "$*" >> "${TELL_LOG:-/dev/null}"; exit 0 ;;
esac
exit 64
CE
chmod +x "$WORK/ce-vacant"
run "$S" PRIMITIVES_COORD_ENGINE="$WORK/ce-vacant"
[ "$RC" = "3" ] && ok "exit 3 on a clean week nobody could have heard about" || bad "expected 3, got $RC"
has "$S/ALERT-UNDELIVERED.txt" "is VACANT" && ok "marker names the vacant role" \
  || bad "no marker: $(cat "$S/ALERT-UNDELIVERED.txt" 2>/dev/null)"

echo
echo "15. the redirect-collapse: a URL that answers with the WRONG page"
# Found live on 2026-07-16: docs.fulcradynamics.com 301s every path to the docs
# ROOT, so /api-reference/ returns the home page at HTTP 200. Each fetch
# succeeds, each page is real, each hash is stable — and docs_api had never once
# observed the page it is named after. -f cannot see this; only the title can.
S="$WORK/t15"; run "$S" PRIMITIVES_DOCS_API_URL="file://$WORK/docs-overview.html"
[ "$RC" = "2" ] && ok "exit 2" || bad "expected 2, got $RC"
[ ! -f "$S/weekly-baseline.json" ] && ok "the home page never becomes docs_api's baseline" || bad "baselined the wrong page"
has "$S/WEEKLY-PROBE-UNKNOWN.txt" "wrong page" && ok "control names it as the wrong document" \
  || bad "wrong-page control did not fire: $(cat "$S/WEEKLY-PROBE-UNKNOWN.txt" 2>/dev/null)"

echo
echo "16. three probes, one document -> the collision control fires"
# The residual case the title control cannot reach: a redirect lands on a page
# whose title happens to satisfy every sentinel. Each probe then passes on its
# own terms — and all three return one hash, which is the thing that is actually
# impossible for three distinct pages.
docs_page "$WORK/docs-omnititle.html" \
  "Fulcra Developer Docs REST API Reference Fulcra MCP Server" "One page for everything."
S="$WORK/t16"; run "$S" \
  PRIMITIVES_DOCS_OVERVIEW_URL="file://$WORK/docs-omnititle.html" \
  PRIMITIVES_DOCS_API_URL="file://$WORK/docs-omnititle.html" \
  PRIMITIVES_DOCS_MCP_URL="file://$WORK/docs-omnititle.html"
[ "$RC" = "2" ] && ok "exit 2" || bad "expected 2, got $RC"
has "$S/WEEKLY-PROBE-UNKNOWN.txt" "same document" && ok "collision named" \
  || bad "collision control did not fire: $(cat "$S/WEEKLY-PROBE-UNKNOWN.txt" 2>/dev/null)"
[ ! -f "$S/weekly-baseline.json" ] && ok "no baseline from a one-document week" || bad "baselined"

echo
echo "17. a page's nav naming another page does not satisfy that page's control"
# The trap a body-text sentinel walks into: every page's nav says "REST API
# Reference", so the home page contains the api page's name.
S="$WORK/t17"; run "$S" PRIMITIVES_DOCS_API_URL="file://$WORK/docs-overview.html"
has "$S/WEEKLY-PROBE-UNKNOWN.txt" "docs_api" && ok "docs_api still fails on the home page" \
  || bad "nav text satisfied the control: $(cat "$S/WEEKLY-PROBE-UNKNOWN.txt" 2>/dev/null)"

echo
echo "18. no probe fallback that type-checks as data survives in the source"
# The idiom, as a grep over this script: `|| echo FAIL`, `.get("paths", {})`,
# `or []`, bare except. This is how all of it happened. `^[^#]*` so the prose
# explaining the bug does not count as committing it again.
SRC="$HERE/weekly-review.sh"
BAD_IDIOM="$(grep -nE '^[^#]*(\|\| *echo *(FAIL|UNKNOWN)|\.get\("paths", *\{\}\)|except: *$|\bor \[\]|\bor "")' "$SRC" || true)"
if [ -n "$BAD_IDIOM" ]; then
  bad "a failure can still be mistaken for an observation: $BAD_IDIOM"
else
  ok "no default-as-observation idiom in weekly-review.sh"
fi

echo
printf 'passed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1

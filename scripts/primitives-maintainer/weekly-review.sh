#!/bin/bash
# fulcra-primitives-maintainer — WEEKLY full-re-read backstop.
#
# The daily drift check fingerprints a narrow, mechanical surface. This weekly
# job casts a wider net — full path+method set, every schema name, docs PROSE,
# the whole MCP discovery doc — and ALWAYS drops a flag asking a session to do a
# genuine human-eyes re-read of FULCRA-PRIMITIVES.md against reality (model
# judgment the shell can't do). The wide fingerprint is a hint about where to
# look; the re-read is the deliverable.
#
# FAIL-CLOSED. Every probe result is one of exactly two things: a real observed
# value, or UNKNOWN. There is no third category — in particular no default that
# type-checks as data. Until 2026-07-16 every probe here fell back to the literal
# string "FAIL", which is a perfectly legal fingerprint value, and got rebaselined
# like any real observation. Two consequences, both silent:
#
#   - TRANSIENT outage: alert once, bake FAIL into the baseline, and then
#     RECOVERY alerts as drift (the real surface differs from "FAIL").
#   - PERSISTENT outage: FAIL == FAIL, "no wide drift", clean forever. A
#     permanently broken probe was indistinguishable from a stable surface.
#
# The docs probes were worse than that. `curl | python` with a dead URL fed the
# hasher an EMPTY STRING, so the fallback was not even "FAIL" — it was
# sha256("")[:16], a stable, real-looking hash that never moves. Three docs pages
# could 404 for a year and this script would report a quiet, healthy week.
#
# So: a probe that cannot answer is UNKNOWN -> alert, exit 2, and the baseline is
# NOT advanced. And every probe is checked against a POSITIVE CONTROL before its
# result is trusted, because an absence of drift means nothing unless the probe
# could have found some. A 404 page, a login interstitial, or a valid OpenAPI doc
# for somebody else's API all hash exactly as cleanly as the real thing.
#
# Runs weekly via launchd: com.fulcra.primitives-maintainer-weekly.plist
set -uo pipefail

# ROOT = the maintainer checkout root (two levels up from scripts/primitives-maintainer/).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Overridable so tests/dry-runs never touch the live baseline.
STATE="${PRIMITIVES_STATE_DIR:-$ROOT/.primitives-state}"
WBASE="$STATE/weekly-baseline.json"
# The re-read-is-owed marker. Dropped every week — the human-eyes pass is due
# whether or not anything moved — and APPENDED to, never truncated, while it is
# still outstanding: once the baseline has advanced past a wide drift, this file
# is the only surviving description of it.
WFLAG="$STATE/WEEKLY-REVIEW-DUE.txt"
# The probe-failure marker. Its own file, NOT the daily's PROBE-UNKNOWN.txt: the
# two jobs share a state dir, and a weekly probe failure discharged by a healthy
# daily run (or vice versa) is a debt cleared by something that never checked it.
WUNKNOWN="$STATE/WEEKLY-PROBE-UNKNOWN.txt"
LOG="$STATE/weekly-review.log"
mkdir -p "$STATE"
ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "$(ts)  $*" >> "$LOG"; }

# Same alert path as the daily — same sender resolution, same role target, same
# reachability check, same rc-checked delivery. It used to be a second copy of
# the same four lines, with the same dead identity baked into it.
. "$(dirname "${BASH_SOURCE[0]}")/lib-alert.sh"
prim_init

SPEC_URL="${PRIMITIVES_SPEC_URL:-https://api.fulcradynamics.com/openapi.json}"
MCP_URL="${PRIMITIVES_MCP_URL:-https://mcp.fulcradynamics.com/.well-known/oauth-authorization-server}"
# The docs ORIGIN, not the vanity domain. Verified live 2026-07-16:
# docs.fulcradynamics.com 301-redirects EVERY path to the docs ROOT, dropping the
# path — /api-reference/, /mcp-server/, even /sitemap.xml all land on the home
# page with HTTP 200. So the three docs probes were fetching ONE document (the
# home page) and hashing it three times: docs_api and docs_mcp have never once
# observed the pages they are named after, and a nonexistent URL produces the
# same clean hash as a real one. That is the same shape as the fingerprint #407
# removed from the daily — a probe structurally incapable of a hit — and it was
# the ONLY mechanical coverage this repo claimed for docs prose.
# (The vanity domain is separately broken for humans: every deep link into
# docs.fulcradynamics.com silently lands on the home page. Worth a platform fix;
# this script points at the origin either way, because a probe should observe the
# thing, not a redirect chain that may or may not preserve the path.)
DOCS_OVERVIEW_URL="${PRIMITIVES_DOCS_OVERVIEW_URL:-https://fulcradynamics.github.io/developer-docs/}"
DOCS_API_URL="${PRIMITIVES_DOCS_API_URL:-https://fulcradynamics.github.io/developer-docs/api-reference/}"
DOCS_MCP_URL="${PRIMITIVES_DOCS_MCP_URL:-https://fulcradynamics.github.io/developer-docs/mcp-server/}"
# Per-page positive controls, checked against the page's <title>. NOT against the
# body text: every page's nav lists every other page, so "REST API Reference"
# appears in the home page's prose — a body-text sentinel would have passed
# happily on all three copies of the home page. The title is the one part of the
# document that identifies WHICH page was served.
DOCS_OVERVIEW_SENTINEL="${PRIMITIVES_DOCS_OVERVIEW_SENTINEL:-Fulcra Developer Docs}"
DOCS_API_SENTINEL="${PRIMITIVES_DOCS_API_SENTINEL:-REST API Reference}"
DOCS_MCP_SENTINEL="${PRIMITIVES_DOCS_MCP_SENTINEL:-Fulcra MCP Server}"
# Positive controls. Same sentinels as the daily where it is the same artifact.
SPEC_SENTINEL="${PRIMITIVES_SPEC_SENTINEL:-/user/v1alpha1/annotation}"
MCP_SENTINEL="${PRIMITIVES_MCP_SENTINEL:-openid}"
# Floor on extracted prose length: an error page or a CDN interstitial strips to
# a handful of words and hashes as stably as a real page. Deliberately low —
# /api-reference/ renders its content client-side from the OpenAPI spec and only
# has ~216 chars of server-side text. That page's hash is therefore a THIN
# observation (it moves on structural change, not on prose); the spec it renders
# is covered properly by wide_spec.
DOCS_MIN_CHARS="${PRIMITIVES_DOCS_MIN_CHARS:-120}"

TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT

UNKNOWN_REASONS=()
unknown() { UNKNOWN_REASONS[${#UNKNOWN_REASONS[@]}]="$1"; }

# Each probe prints "OK <value>" or "FAIL <reason>" and nothing else — the
# daily's protocol. The caller does the UNKNOWN bookkeeping, so no probe can
# quietly hand back a value it did not observe.
#
# NB: this sets TAKE_VALUE rather than echoing it, because `X="$(take ...)"` runs
# take in a SUBSHELL and every UNKNOWN_REASONS append inside it is discarded on
# exit — the probe would fail, the reason would evaporate, and "UNKNOWN" would
# sail into the fingerprint as a value with nothing recorded against it. That is
# this script's own bug wearing a different hat.
# $1 label, $2 the probe's raw output -> sets TAKE_VALUE.
TAKE_VALUE=""
take() {
  local label="$1" out="$2"
  case "$out" in
    "OK "*)   TAKE_VALUE="${out#OK }" ;;
    "FAIL "*) unknown "$label: ${out#FAIL }"; TAKE_VALUE="UNKNOWN" ;;
    *)        unknown "$label: probe produced no usable output (python3 missing, or it crashed before reporting)"
              TAKE_VALUE="UNKNOWN" ;;
  esac
}

# --- probe: wide spec fingerprint -----------------------------------------
# Full sorted path:method tuples + every schema name.
probe_wide_spec() {
  local f="$TMPDIR_RUN/openapi.json" rc
  curl -fsL --max-time 20 "$SPEC_URL" -o "$f"; rc=$?
  if [ $rc -ne 0 ]; then
    echo "FAIL fetch failed (curl rc=$rc, $SPEC_URL)"; return
  fi
  SENT="$SPEC_SENTINEL" python3 - "$f" <<'PY' || echo "FAIL probe crashed (stderr is in the job's launchd err log)"
import sys, json, hashlib, os

try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print("FAIL not parseable JSON (%s)" % (e,)); raise SystemExit(0)
# `d.get("paths", {})` was the bug: a doc with no paths at all hashed to a real,
# stable value. An absent key is a failed observation, not an empty API.
if not isinstance(d.get("paths"), dict) or not d["paths"]:
    print("FAIL no paths in the fetched document"); raise SystemExit(0)
paths = d["paths"]
sent = os.environ.get("SENT", "")
# Positive control. A valid-but-WRONG OpenAPI doc — someone else's API, a staging
# stub, an error page that happens to be JSON — hashes exactly as cleanly as ours
# and would then be rebaselined as the new truth.
if sent and sent not in paths:
    print("FAIL positive control — sentinel path %s absent from the fetched spec "
          "(%d paths: this is not our API)" % (sent, len(paths))); raise SystemExit(0)
# The `{}` defaults here are safe where `paths` was not, and the difference is
# the control above: we have already proven this document is OUR spec (sentinel
# path present) and that it parsed. So an absent components.schemas is a real
# observation about a real spec — an API with no schemas — not a fetch that
# failed. A present-but-wrong-shaped one still fails closed.
schemas = d.get("components", {}).get("schemas", {})
if not isinstance(schemas, dict):
    print("FAIL components.schemas is not a mapping"); raise SystemExit(0)
blob = json.dumps({"methods": sorted("%s:%s" % (p, m) for p in paths for m in paths[p]),
                   "schemas": sorted(schemas.keys())}, sort_keys=True)
print("OK " + hashlib.sha256(blob.encode()).hexdigest()[:16])
PY
}

# --- probe: docs prose ------------------------------------------------------
# Content hash of a docs page's visible text — catches prose the daily ignores.
# $1 url, $2 the title fragment that identifies THIS page.
probe_docs() {
  local url="$1" sent="$2" f="$TMPDIR_RUN/docs-$$-$RANDOM.html" rc
  # -f so an HTTP error is a failed fetch. Without it a 404 body is "content" and
  # hashes as stably as the page it replaced. Note -f does NOT save us from the
  # redirect-to-home case: that returns a real 200 for a real page — just not the
  # one we asked for. Only the title control below catches that.
  curl -fsL --max-time 15 "$url" -o "$f"; rc=$?
  if [ $rc -ne 0 ]; then
    echo "FAIL fetch failed (curl rc=$rc, $url)"; return
  fi
  SENT="$sent" MIN="$DOCS_MIN_CHARS" URL="$url" python3 - "$f" <<'PY' || echo "FAIL probe crashed (stderr is in the job's launchd err log)"
import sys, re, hashlib, os

try:
    raw = open(sys.argv[1], "rb").read().decode("utf-8", "replace")
except Exception as e:
    print("FAIL could not read the fetched page (%s)" % (e,)); raise SystemExit(0)

sent = os.environ.get("SENT", "")
if sent:
    # WHICH page did we actually get? The vanity domain answers every path with
    # the home page at HTTP 200, so "the fetch succeeded" says nothing about what
    # was served, and every page's nav mentions every other page's name — so this
    # control reads the <title>, the one element that identifies the document.
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.S | re.I)
    if not m:
        print("FAIL positive control — fetched document has no <title> at all "
              "(%s)" % os.environ.get("URL", "")); raise SystemExit(0)
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    if sent.lower() not in title.lower():
        print("FAIL positive control — wrong page: title %r does not contain %r. The "
              "URL answered, but with somebody else's document (a redirect that drops "
              "the path serves the home page for every request)." % (title[:90], sent))
        raise SystemExit(0)

t = re.sub(r"<(script|style).*?</\1>", "", raw, flags=re.S | re.I)
t = re.sub(r"<[^>]+>", " ", t)
t = re.sub(r"\s+", " ", t).strip()
# The original fed an EMPTY string to the hasher on a dead URL and printed
# sha256("") — a stable, legitimate-looking hash that never moves again.
if not t:
    print("FAIL page stripped to no text at all"); raise SystemExit(0)
try:
    floor = int(os.environ.get("MIN", "0"))
except ValueError:
    floor = 0
if len(t) < floor:
    print("FAIL positive control — only %d chars of prose (< %d): an error page or an "
          "empty shell, not a docs page: %r" % (len(t), floor, t[:80]))
    raise SystemExit(0)
print("OK " + hashlib.sha256(t.encode()).hexdigest()[:16])
PY
}

# --- probe: MCP discovery ---------------------------------------------------
# The WHOLE discovery doc, not just the scopes the daily reads.
probe_mcp() {
  local f="$TMPDIR_RUN/mcp.json" rc
  curl -fsL --max-time 12 "$MCP_URL" -o "$f"; rc=$?
  if [ $rc -ne 0 ]; then
    echo "FAIL fetch failed (curl rc=$rc, $MCP_URL)"; return
  fi
  SENT="$MCP_SENTINEL" python3 - "$f" <<'PY' || echo "FAIL probe crashed (stderr is in the job's launchd err log)"
import sys, json, hashlib, os

try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print("FAIL not parseable JSON (%s)" % (e,)); raise SystemExit(0)
if not isinstance(d, dict) or not d:
    print("FAIL discovery doc is not a non-empty object"); raise SystemExit(0)
sent = os.environ.get("SENT", "")
if sent:
    scopes = d.get("scopes_supported")
    if not isinstance(scopes, list):
        print("FAIL no scopes_supported list in the fetched discovery doc")
        raise SystemExit(0)
    if sent not in scopes:
        print("FAIL positive control — sentinel scope %s absent from the fetched "
              "discovery doc (%s)" % (sent, ",".join(map(str, scopes))))
        raise SystemExit(0)
print("OK " + hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16])
PY
}

take wide_spec     "$(probe_wide_spec)"; WIDE_SPEC="$TAKE_VALUE"
take mcp_disc      "$(probe_mcp)";       MCP_DISC="$TAKE_VALUE"
take docs_overview "$(probe_docs "$DOCS_OVERVIEW_URL" "$DOCS_OVERVIEW_SENTINEL")"; DOCS_OVERVIEW="$TAKE_VALUE"
take docs_api      "$(probe_docs "$DOCS_API_URL" "$DOCS_API_SENTINEL")";           DOCS_API="$TAKE_VALUE"
take docs_mcp      "$(probe_docs "$DOCS_MCP_URL" "$DOCS_MCP_SENTINEL")";           DOCS_MCP="$TAKE_VALUE"

# Cross-probe control: three probes that agree exactly are not three probes.
# Distinct pages cannot hash identically, so a collision means they are all
# reading one document — which is precisely what a path-dropping redirect does,
# and it is invisible to any per-probe check (each fetch succeeds, each page is
# real, each hash is stable). Three fingerprints, one surface, and the week reads
# clean forever. This control is the reason that gets noticed.
docs_collision() { # $1 $2 labels, $3 $4 their values
  local a="$1" b="$2" va="$3" vb="$4"
  # An UNKNOWN pair is already accounted for; only two OBSERVED equal hashes are
  # the tell.
  [ "$va" = "UNKNOWN" ] && return 0
  [ "$vb" = "UNKNOWN" ] && return 0
  if [ "$va" = "$vb" ]; then
    unknown "$a + $b: both probes hashed to the same document ($va). Two different docs pages cannot be byte-identical — these probes are not observing the pages they are named after (a redirect that drops the path answers every URL with the home page)."
  fi
  return 0
}
docs_collision docs_overview docs_api "$DOCS_OVERVIEW" "$DOCS_API"
docs_collision docs_overview docs_mcp "$DOCS_OVERVIEW" "$DOCS_MCP"
docs_collision docs_api      docs_mcp "$DOCS_API"      "$DOCS_MCP"

CUR="$(python3 -c "import json,sys;print(json.dumps(dict(zip(['wide_spec','docs_overview','docs_api','docs_mcp','mcp_disc'],sys.argv[1:6])),sort_keys=True))" \
  "$WIDE_SPEC" "$DOCS_OVERVIEW" "$DOCS_API" "$DOCS_MCP" "$MCP_DISC")"

# --- UNKNOWN: fail closed ---------------------------------------------------
# Baseline untouched. An UNKNOWN run observed nothing, so it has nothing to say
# about last week's surface — and it must never write "UNKNOWN" (or "FAIL", or
# sha256("")) where next week's comparison will read it as an observation.
if [ ${#UNKNOWN_REASONS[@]} -gt 0 ]; then
  {
    echo "WEEKLY WIDE CHECK UNKNOWN $(ts)"
    echo "One or more probes could not answer. This is NOT 'no wide drift' — the"
    echo "surface was not observed. Baseline NOT advanced; this repeats until the"
    echo "probes work."
    echo
    printf '%s\n' "${UNKNOWN_REASONS[@]}"
    echo
    echo "PARTIAL: $CUR"
    echo
    echo "The human-eyes re-read ($WFLAG) is due regardless and is unaffected by"
    echo "this file. The next run whose probes answer clears this one for you."
  } > "$WUNKNOWN"
  log "UNKNOWN: ${UNKNOWN_REASONS[*]}"
  # Carry any outstanding re-read into THIS alert, and say so. Without this, an
  # UNKNOWN week silently discharges an older debt: if a previous WIDE DRIFT tell
  # failed, WFLAG and ALERT-UNDELIVERED.txt both survived — but a successful
  # UNKNOWN tell to a recovered target would clear the marker, which recorded that
  # the WIDE DRIFT payload was never delivered. A debt is not paid by the delivery
  # of a different message about a different subject. The flag file is read here
  # BEFORE this run appends to it (that happens further down), so its presence
  # means a PREVIOUS week's re-read is still owed. Same guard as the daily's
  # UNKNOWN branch (drift-check.sh).
  WOUTSTANDING_NOTE=""
  [ -f "$WFLAG" ] && WOUTSTANDING_NOTE=" SEPARATELY: $WFLAG is still outstanding from an earlier week (its wide drift, if any, was never actioned and the baseline has moved past it) and is untouched by this run — that re-read is still owed. Flag contents: $(head -c 500 "$WFLAG" | tr '\n' ' ')"
  prim_notify P1 "UNKNOWN: Fulcra primitives weekly wide check could not observe the surface ($(ts))" \
    "$(printf '%s; ' "${UNKNOWN_REASONS[@]}")Baseline NOT advanced — a probe that cannot answer is not a clean week. Fix the probe, then re-run scripts/primitives-maintainer/weekly-review.sh. Probe-failure file: $WUNKNOWN.$WOUTSTANDING_NOTE"
  # The re-read flag still drops: it is owed every week no matter what the probes
  # did, and a session doing it by hand is exactly what an UNKNOWN week needs.
  DRIFT_NOTE="wide fingerprint: UNKNOWN this week — probes could not observe the surface (see $WUNKNOWN). Re-read against the live surface by hand; do not trust a hash to have covered it."
else
  if [ -f "$WUNKNOWN" ]; then
    log "probes recovered; clearing $WUNKNOWN"
    rm -f "$WUNKNOWN"
  fi
  DRIFT_NOTE=""
fi

# --- compare ----------------------------------------------------------------
CHANGED=""
RENDER_FAILED=""
if [ ${#UNKNOWN_REASONS[@]} -eq 0 ] && [ -f "$WBASE" ]; then
  PREV="$(cat "$WBASE")"
  if [ "$CUR" != "$PREV" ]; then
    CHANGED="yes"
    # Name the fields, so the flag survives the baseline moving past it.
    # The default below describes, it does not decide: CHANGED was already
    # settled by exact string equality above, independently of this renderer.
    DRIFT_NOTE="$(PREV="$PREV" CUR="$CUR" python3 - <<'PY' 2>/dev/null || echo "RENDERER FAILED — wide fingerprint changed but the change could not be characterized"
import json, os
prev, cur = json.loads(os.environ["PREV"]), json.loads(os.environ["CUR"])
lines = [f"{k}: {prev.get(k)} -> {cur.get(k)}"
         for k in sorted(set(prev) | set(cur)) if prev.get(k) != cur.get(k)]
print("\n".join(lines) or "RENDERER FAILED — no field diff could be rendered")
PY
)"
    [ -n "$DRIFT_NOTE" ] || DRIFT_NOTE="RENDERER FAILED — the diff renderer produced nothing"
    case "$DRIFT_NOTE" in
      "RENDERER FAILED"*) RENDER_FAILED="yes" ;;
    esac
  fi
fi

# --- the flag ---------------------------------------------------------------
# Always dropped: the re-read is owed weekly. But APPEND while it is still
# outstanding — a flag still sitting here means last week's re-read never
# happened, and truncating it would erase the only record of what last week's
# wide drift was, since the baseline has already moved past it.
OUTSTANDING=""
if [ -f "$WFLAG" ]; then
  OUTSTANDING="yes"
  {
    echo
    echo "==================================================================="
    echo "STILL OUTSTANDING — everything above was never actioned. The baseline"
    echo "has already moved past it, so this file is the only record."
    echo "==================================================================="
  } >> "$WFLAG"
fi
{
  echo "WEEKLY FULL RE-READ DUE $(ts)"
  echo "A session should: re-read FULCRA-PRIMITIVES.md end-to-end vs the live"
  echo "API spec, CLI --help, docs.fulcradynamics.com pages, and the MCP server,"
  echo "fix any drift, bump the 'Verified against' header, push doc-only to main."
  if [ -n "$DRIFT_NOTE" ]; then
    echo
    echo "WHAT MOVED (or could not be seen) THIS WEEK:"
    printf '%s\n' "$DRIFT_NOTE"
  fi
  echo
  echo "wide fingerprint NOW: $CUR"
  echo
  echo "Clear this file (rm $WFLAG) once the re-read is done — for EVERY entry in"
  echo "it, not just the last one."
} >> "$WFLAG"

# --- advance the baseline ---------------------------------------------------
# Only a fully observed week may move it. This is the whole fix: "FAIL" and
# sha256("") used to be written here as though they were observations, and then
# compared against next week as though they were the truth.
if [ ${#UNKNOWN_REASONS[@]} -gt 0 ]; then
  log "baseline NOT advanced (UNKNOWN week); flag dropped${OUTSTANDING:+ (previous re-read still outstanding)}"
  finish 2 "UNKNOWN — weekly probes could not observe the surface: ${UNKNOWN_REASONS[*]}"
fi
# A drift we could not describe is not a drift we may rebaseline away: if we
# cannot say WHAT changed, we do not get to forget that it changed. Same rule as
# the daily.
if [ -n "$RENDER_FAILED" ]; then
  log "baseline NOT advanced: the wide change could not be characterized"
else
  echo "$CUR" > "$WBASE"
fi

# 1 = something is owed beyond the routine weekly re-read (same register as the
# daily: 0 clean, 1 owed, 2 UNKNOWN, 3 the alert path could not deliver).
RC=0
[ -n "$OUTSTANDING" ] && RC=1
if [ -n "$CHANGED" ]; then
  log "WIDE DRIFT: $(printf '%s' "$DRIFT_NOTE" | tr '\n' '; ')"
  prim_notify P2 "WEEKLY WIDE-DRIFT: full human-eyes re-read of FULCRA-PRIMITIVES.md needed ($(ts))" \
    "Broad fingerprint changed — new endpoints/schemas, docs prose, or MCP surface the daily narrow check misses. WHAT MOVED: $(printf '%s' "$DRIFT_NOTE" | tr '\n' '; ') Re-read FULCRA-PRIMITIVES.md end-to-end, then rm $WFLAG. Flag file: $WFLAG."
  finish 1 "WIDE DRIFT: $(printf '%s' "$DRIFT_NOTE" | tr '\n' '; ')"
fi
# An unchanged week does NOT discharge an outstanding flag — it is exactly the
# run that used to lose it. If a previous week's WIDE DRIFT notification failed,
# WFLAG survived, but this path only set RC=1 and finished WITHOUT notifying;
# once the target recovered, prim_flush cleared ALERT-UNDELIVERED.txt for a
# payload nobody ever received. The debt evaporated on recovery. So: re-alert the
# owed flag, and let prim_flush clear the marker only if THAT tell is proven
# delivered. Same shape as the daily's outstanding branch (drift-check.sh).
if [ -n "$OUTSTANDING" ]; then
  log "no wide drift, but the weekly re-read is STILL OUTSTANDING: $WFLAG"
  prim_notify P1 "OUTSTANDING: Fulcra primitives weekly re-read still unactioned ($(ts))" \
    "No wide drift vs last week, but $WFLAG is still present — an earlier weekly re-read (and any wide drift it recorded) was never actioned, and the baseline has already moved past it, so that file is the only record. Re-read FULCRA-PRIMITIVES.md end-to-end for EVERY entry in it, then rm $WFLAG. Flag contents: $(head -c 800 "$WFLAG" | tr '\n' ' ')"
  finish "$RC" "OUTSTANDING — an earlier weekly re-read is still unactioned ($WFLAG)"
fi
log "weekly review flag dropped; no wide drift vs last week ($CUR)"
finish "$RC"

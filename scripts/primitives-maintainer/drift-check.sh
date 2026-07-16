#!/bin/bash
# fulcra-primitives-maintainer — daily drift detector.
#
# Fingerprints the agent-facing Fulcra surface and compares it to a stored
# baseline. On drift (or on an unusable probe) it posts to the coord team bus
# (team fulcra) as fulcra-primitives-maintainer and writes an ALERT file so a
# Claude session does the actual FULCRA-PRIMITIVES.md rewrite. Detection is
# unattended; the rewrite (model judgment) is not.
#
# WHAT IT OBSERVES (see README for the full claim/limit table):
#   pypi_version    published fulcra-api version on PyPI.
#   cli_verbs       top-level verb list of the PUBLISHED CLI, parsed from
#                   `fulcra-api --help` of that exact version. This is the
#                   fingerprint that carries the documented full-rewrite
#                   trigger: `record` and `delete` are top-level verbs, so a
#                   record/delete verb landing (or vanishing) moves this.
#   cli_groups      subcommands of every top-level verb that is a group
#                   (auth, data-type, file, share, tag, + anything new —
#                   groups are discovered, not hardcoded).
#   cli_beta_verbs  verbs visible only under `--beta`.
#   spec_hash       sha of the FULL path->methods map of the published
#                   OpenAPI spec + DataRecordV1 presence.
#   mcp_scopes      OAuth `scopes_supported` from MCP discovery.
#   cli_head        fulcra-api-python main HEAD sha (pre-release early warning).
#
# WHAT IT CANNOT OBSERVE (do not read a clean run as covering these):
#   - MCP TOOL LIST. mcp_scopes is OAuth scopes only. A new MCP write tool
#     under an existing scope does not move it. Nothing here sees tools/list
#     (it needs an authenticated session). weekly-review.sh's human-eyes pass
#     is the only coverage.
#   - DATASHARE ENDPOINTS. The datashare REST surface behind the `share` verb
#     is not published in openapi.json at all (verified 2026-07-16: 53 paths,
#     zero datashare paths), so spec_hash cannot see it. The CLI `share` group
#     in cli_groups is the only coverage.
#   - Docs prose, semantics behind an unchanged signature, and anything the
#     CLI/spec do not expose. weekly-review.sh covers the first.
#
# FAIL-CLOSED. Every probe is checked against a positive control before any
# result is trusted (see check_positive_controls). A probe that returns
# empty/unparseable is UNKNOWN -> alert, never "no drift", and UNKNOWN never
# advances the baseline. The failure this rebuild exists to fix (2026-07-16)
# was a fingerprint that was structurally incapable of producing a hit, so a
# scan whose empty result is not proven meaningful is treated as a failure.
#
# Runs daily via launchd: com.fulcra.primitives-maintainer.plist
set -uo pipefail

# ROOT = the maintainer checkout root (two levels up from scripts/primitives-maintainer/).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Overridable so tests/dry-runs never touch the live baseline.
STATE="${PRIMITIVES_STATE_DIR:-$ROOT/.primitives-state}"
BASELINE="$STATE/baseline.json"
ALERT="$STATE/DRIFT-ALERT.txt"
LOG="$STATE/drift-check.log"
# Overridable so a test run can capture the notification instead of posting it.
CE="${PRIMITIVES_COORD_ENGINE:-$(command -v coord-engine || echo "$HOME/.local/bin/coord-engine")}"
TEAM="fulcra"
AGENT="claude-code:Mac:fulcra-primitives-maintainer"

SPEC_URL="${PRIMITIVES_SPEC_URL:-https://api.fulcradynamics.com/openapi.json}"
MCP_URL="${PRIMITIVES_MCP_URL:-https://mcp.fulcradynamics.com/.well-known/oauth-authorization-server}"
PYPI_URL="${PRIMITIVES_PYPI_URL:-https://pypi.org/pypi/fulcra-api/json}"
# Set to 1 on hosts with no gh auth. An explicit, visible opt-out — an absent
# probe must be a decision on the record, not a silent pass.
SKIP_GH="${PRIMITIVES_SKIP_GH:-0}"
# Sentinel verbs that must be present in any correctly-parsed CLI help. If
# these are missing the parser is broken or the package did not run; that is
# UNKNOWN, not "the CLI lost auth".
CLI_SENTINELS="${PRIMITIVES_CLI_SENTINELS:-auth,user-info,catalog}"
# Sentinel path that must be present in any correctly-fetched spec.
SPEC_SENTINEL="/user/v1alpha1/annotation"

mkdir -p "$STATE"
ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "$(ts)  $*" >> "$LOG"; }

TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT
SPEC="$TMPDIR_RUN/openapi.json"

# --- probe: PyPI published version ---------------------------------------
PYPI_VER="$(curl -s --max-time 20 "$PYPI_URL" 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null \
  || true)"
[ -n "$PYPI_VER" ] || PYPI_VER="UNKNOWN"

# --- probe: published CLI surface ----------------------------------------
# Fingerprints what an agent actually gets from `uv tool install fulcra-api`,
# NOT a decorator grep against one file in git main. The old fingerprint
# counted `@data_type.command` in fulcra_api/cli/data_types.py, which could
# not see `record`/`delete` (top-level, different file, no such decorator) —
# it missed 0.1.37 and 0.1.36 outright.
CLI_JSON="UNKNOWN"
if [ "$PYPI_VER" != "UNKNOWN" ] && command -v uvx >/dev/null 2>&1; then
  CLI_JSON="$(PYPI_VER="$PYPI_VER" python3 - <<'PY' 2>/dev/null || echo UNKNOWN
import json, os, re, subprocess

ver = os.environ["PYPI_VER"]
BASE = ["uvx", "-q", "--from", f"fulcra-api=={ver}", "fulcra-api"]


def help_text(args):
    try:
        r = subprocess.run(BASE + args + ["--help"], capture_output=True,
                           text=True, timeout=120)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def verbs(text):
    """Parse a click 'Commands:' block. Command lines are exactly two spaces
    then the name; wrapped descriptions are indented far deeper."""
    if not text or "Commands:" not in text:
        return None
    block = text.split("Commands:", 1)[1]
    out = []
    for line in block.splitlines():
        m = re.match(r"^ {2}(\S+)", line)
        if m:
            out.append(m.group(1))
        elif line.strip() and not line.startswith(" "):
            break
    return sorted(set(out))


top = verbs(help_text([]))
if not top:
    raise SystemExit(1)

groups = {}
for v in top:
    sub = verbs(help_text([v]))
    if sub:
        groups[v] = sub

beta = verbs(help_text(["--beta"])) or []
print(json.dumps({
    "cli_verbs": top,
    "cli_groups": groups,
    "cli_beta_verbs": sorted(set(beta) - set(top)),
}, sort_keys=True))
PY
)"
fi
[ -n "$CLI_JSON" ] || CLI_JSON="UNKNOWN"

# --- probe: OpenAPI spec --------------------------------------------------
curl -s --max-time 20 "$SPEC_URL" -o "$SPEC" 2>/dev/null
SPEC_OUT="$(python3 - "$SPEC" <<'PY' 2>/dev/null || echo "UNKNOWN {}"
import sys, json, hashlib
try:
    d = json.load(open(sys.argv[1]))
    paths = d["paths"]
    if not paths:
        raise ValueError("empty paths")
except Exception:
    print("UNKNOWN {}")
    sys.exit(0)
# Full path->methods map, not a hand-picked subset. The old sig only looked at
# paths containing "annotation" and paths under /ingest/, plus a path_count
# that does not move when a path is swapped 1-for-1 — so the catalog-schema
# endpoints moved without moving the fingerprint. Any added, removed, or
# re-methoded endpoint moves this hash.
sig = {
    "paths": {p: sorted(paths[p].keys()) for p in sorted(paths)},
    "path_count": len(paths),
    "has_DataRecordV1": "DataRecordV1" in d.get("components", {}).get("schemas", {}),
}
print(hashlib.sha256(json.dumps(sig, sort_keys=True).encode()).hexdigest()[:16]
      + " " + json.dumps(sig, sort_keys=True))
PY
)"
SPEC_HASH="${SPEC_OUT%% *}"; SPEC_SIG="${SPEC_OUT#* }"

# --- probe: MCP OAuth scopes ---------------------------------------------
# OAuth scopes ONLY. This cannot see the MCP tool list; a new MCP write tool
# under an existing scope will not move it. The old header claimed this
# fingerprinted "MCP discovery" — it does not.
MCP_SCOPES="$(curl -s --max-time 12 "$MCP_URL" 2>/dev/null \
  | python3 -c 'import sys,json;print(",".join(json.load(sys.stdin).get("scopes_supported",[])))' 2>/dev/null \
  || true)"
[ -n "$MCP_SCOPES" ] || MCP_SCOPES="UNKNOWN"

# --- probe: fulcra-api-python main HEAD ----------------------------------
# Early warning only: the CLI ships to PyPI ahead of git main, so this is not
# the release signal. pypi_version + cli_verbs are.
if [ "$SKIP_GH" = "1" ]; then
  CLI_HEAD="disabled"
else
  CLI_HEAD="$(gh api repos/fulcradynamics/fulcra-api-python/commits/main --jq '.sha[:7]' 2>/dev/null || true)"
  [ -n "$CLI_HEAD" ] || CLI_HEAD="UNKNOWN"
fi

# --- positive control ------------------------------------------------------
# Prove each probe CAN produce a hit before trusting an absence of drift.
UNKNOWN_REASONS=()
[ "$PYPI_VER" = "UNKNOWN" ] && UNKNOWN_REASONS+=("pypi_version: fetch/parse failed ($PYPI_URL)")
[ "$SPEC_HASH" = "UNKNOWN" ] && UNKNOWN_REASONS+=("spec_hash: fetch/parse failed or empty paths ($SPEC_URL)")
[ "$MCP_SCOPES" = "UNKNOWN" ] && UNKNOWN_REASONS+=("mcp_scopes: fetch/parse failed ($MCP_URL)")
[ "$CLI_HEAD" = "UNKNOWN" ] && UNKNOWN_REASONS+=("cli_head: gh api failed (set PRIMITIVES_SKIP_GH=1 to opt out explicitly)")
if [ "$CLI_JSON" = "UNKNOWN" ]; then
  UNKNOWN_REASONS+=("cli_verbs: could not run/parse published fulcra-api --help (uvx present? version resolvable?)")
else
  # The parse must contain known-stable verbs, or the parser — not the CLI —
  # is what changed.
  MISSING="$(CLI_JSON="$CLI_JSON" SENT="$CLI_SENTINELS" python3 -c '
import json, os
try:
    v = set(json.loads(os.environ["CLI_JSON"])["cli_verbs"])
except Exception:
    print("PARSE"); raise SystemExit(0)
print(",".join(sorted(s for s in os.environ["SENT"].split(",") if s not in v)))
' 2>/dev/null || echo PARSE)"
  [ -n "$MISSING" ] && UNKNOWN_REASONS+=("cli_verbs: positive control failed — sentinel verbs missing/unparseable: $MISSING")
fi
if [ "$SPEC_HASH" != "UNKNOWN" ] && ! grep -q "\"$SPEC_SENTINEL\"" "$SPEC" 2>/dev/null; then
  UNKNOWN_REASONS+=("spec_hash: positive control failed — sentinel path $SPEC_SENTINEL absent from fetched spec")
fi

# --- assemble fingerprint --------------------------------------------------
CUR="$(CLI_JSON="$CLI_JSON" python3 -c '
import json, os, sys
cli = os.environ["CLI_JSON"]
try:
    cli = json.loads(cli)
except Exception:
    cli = {"cli_verbs": "UNKNOWN", "cli_groups": "UNKNOWN", "cli_beta_verbs": "UNKNOWN"}
d = {
    "spec_hash": sys.argv[1],
    "cli_head": sys.argv[2],
    "mcp_scopes": sys.argv[3],
    "pypi_version": sys.argv[4],
}
d.update(cli)
print(json.dumps(d, sort_keys=True))
' "$SPEC_HASH" "$CLI_HEAD" "$MCP_SCOPES" "$PYPI_VER")"

# --- notify helper ---------------------------------------------------------
# `tell` drops a message (rc 1, "already exists") when a slug prefix collides,
# so the title carries a timestamp and the rc is checked and logged.
notify() {
  local prio="$1" title="$2" summary="$3" out rc
  out="$("$CE" tell "$TEAM" "$AGENT" "$title" --from "$AGENT" \
        --workstream fulcra-primitives --priority "$prio" --summary "$summary" 2>&1)"
  rc=$?
  if [ $rc -ne 0 ]; then
    log "TELL FAILED rc=$rc: $out"
  else
    log "tell ok: $title"
  fi
  return $rc
}

# --- UNKNOWN: fail closed --------------------------------------------------
if [ ${#UNKNOWN_REASONS[@]} -gt 0 ]; then
  {
    echo "DRIFT CHECK UNKNOWN $(ts)"
    echo "One or more probes could not answer. This is NOT 'no drift' — the"
    echo "surface was not observed. Baseline NOT advanced; this run will repeat"
    echo "until the probes work or the fingerprint is fixed."
    echo
    printf '%s\n' "${UNKNOWN_REASONS[@]}"
    echo
    echo "PARTIAL: $CUR"
  } > "$ALERT"
  log "UNKNOWN: ${UNKNOWN_REASONS[*]}"
  notify P1 "UNKNOWN: Fulcra primitives drift check could not observe the surface ($(ts))" \
    "$(printf '%s; ' "${UNKNOWN_REASONS[@]}")Baseline NOT advanced — a probe that cannot answer is not a clean run. Fix the probe or the fingerprint, then re-run scripts/primitives-maintainer/drift-check.sh. Alert file: $ALERT."
  exit 2
fi

# --- first run: write baseline, done ---------------------------------------
if [ ! -f "$BASELINE" ]; then
  echo "$CUR" > "$BASELINE"
  log "baseline written: $CUR"
  exit 0
fi

PREV="$(cat "$BASELINE")"

# --- compare ---------------------------------------------------------------
DIFF="$(PREV="$PREV" CUR="$CUR" python3 - <<'PY'
import json, os

prev = json.loads(os.environ["PREV"])
cur = json.loads(os.environ["CUR"])
lines = []

def listdiff(label, a, b):
    if not isinstance(a, list) or not isinstance(b, list):
        if a != b:
            lines.append(f"{label}: {a!r} -> {b!r}")
        return
    added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
    if added:
        lines.append(f"{label}: ADDED {', '.join(added)}")
    if removed:
        lines.append(f"{label}: REMOVED {', '.join(removed)}")

for k in ("pypi_version", "cli_head", "spec_hash", "mcp_scopes"):
    if prev.get(k) != cur.get(k):
        lines.append(f"{k}: {prev.get(k)} -> {cur.get(k)}")
listdiff("cli_verbs", prev.get("cli_verbs"), cur.get("cli_verbs"))
listdiff("cli_beta_verbs", prev.get("cli_beta_verbs"), cur.get("cli_beta_verbs"))

pg, cg = prev.get("cli_groups") or {}, cur.get("cli_groups") or {}
if isinstance(pg, dict) and isinstance(cg, dict):
    for g in sorted(set(pg) | set(cg)):
        listdiff(f"cli_groups[{g}]", pg.get(g, []), cg.get(g, []))
else:
    listdiff("cli_groups", pg, cg)

print("\n".join(lines) or "fingerprint changed but no field diff could be rendered")
PY
)"
# A drift we cannot describe is not a drift we may rebaseline away.
if [ -z "$DIFF" ]; then
  DIFF="DIFF RENDERER FAILED — fingerprint changed but the change could not be characterized"
fi

# Name the trigger explicitly when a write verb moves, so nobody is routed to
# the wrong file. The 2026-07-16 miss sent an agent to data_types.py to find a
# `record` verb that lives one directory up.
TRIGGER="$(printf '%s\n' "$DIFF" | grep -E '^cli_verbs: (ADDED|REMOVED).*\b(record|delete)\b' || true)"

if [ "$CUR" = "$PREV" ]; then
  if [ -f "$ALERT" ]; then
    # An earlier drift is still unactioned. A clean fingerprint does not clear
    # the debt: rebaselining silences the field, it does not do the rewrite.
    log "no new drift, but UNACKED alert outstanding: $ALERT"
    notify P1 "OUTSTANDING: Fulcra primitives drift still unactioned ($(ts))" \
      "No new drift vs baseline, but $ALERT is still present — a previous drift has not been actioned. Do the FULCRA-PRIMITIVES.md rewrite, then \`rm $ALERT\` to clear. Alert contents: $(head -c 800 "$ALERT" | tr '\n' ' ')"
    exit 1
  fi
  log "no drift (pypi=$PYPI_VER spec=$SPEC_HASH cli_head=$CLI_HEAD)"
  exit 0
fi

# --- drift -----------------------------------------------------------------
{
  echo "DRIFT DETECTED $(ts)"
  echo
  echo "WHAT CHANGED:"
  printf '%s\n' "$DIFF"
  if [ -n "$TRIGGER" ]; then
    echo
    echo "*** FULL-REWRITE TRIGGER: a top-level record/delete verb moved in the"
    echo "*** published CLI (fulcra-api $PYPI_VER). These are TOP-LEVEL verbs —"
    echo "*** do not go looking for them in fulcra_api/cli/data_types.py."
  fi
  echo
  echo "PREV: $PREV"
  echo "NOW:  $CUR"
  echo "spec_sig: $SPEC_SIG"
  echo
  echo "Clear this file (rm $ALERT) once FULCRA-PRIMITIVES.md is rewritten."
} > "$ALERT"
log "DRIFT: $(printf '%s' "$DIFF" | tr '\n' '; ')"

SUMMARY="WHAT CHANGED (fulcra-api $PYPI_VER): $(printf '%s' "$DIFF" | tr '\n' '; ')"
if [ -n "$TRIGGER" ]; then
  SUMMARY="FULL-REWRITE TRIGGER — a top-level record/delete verb moved in the published CLI. $SUMMARY Note: record/delete are TOP-LEVEL verbs, not data_types.py subcommands. Tier-2 API-direct guidance shifts too."
  PRIO=P1
else
  PRIO=P2
fi
notify "$PRIO" "DRIFT: Fulcra primitives surface changed ($(ts))" \
  "$SUMMARY Re-verify + rewrite FULCRA-PRIMITIVES.md, then rm $ALERT. Alert file: $ALERT."

# Advance the baseline only now that the change is characterized in the alert
# and named in the tell. The ALERT file stays until a session clears it, so an
# unactioned drift keeps being reported (see the no-drift branch above) —
# rebaselining stops repeat *discovery* noise, not the outstanding work.
# If we could not say WHAT changed, we do not get to forget that it changed.
case "$DIFF" in
  "DIFF RENDERER FAILED"*)
    log "baseline NOT advanced: change could not be characterized"
    ;;
  *)
    echo "$CUR" > "$BASELINE"
    ;;
esac
exit 1

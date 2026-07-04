#!/bin/bash
# fulcra-primitives-maintainer — daily drift detector.
# Fingerprints the live Fulcra surface (OpenAPI spec, CLI HEAD, annotation
# commands, MCP discovery) and compares to a stored baseline. On drift it
# posts to the coord2 team bus (team fulcra) as fulcra-primitives-maintainer and writes an
# ALERT file so a Claude session does the actual FULCRA-PRIMITIVES.md rewrite.
# Detection is unattended; the rewrite (model judgment) is not.
#
# Runs daily via launchd: com.fulcra.primitives-maintainer.plist
set -uo pipefail

# ROOT = the maintainer checkout root (two levels up from scripts/primitives-maintainer/).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE="$ROOT/.primitives-state"        # local runtime state (gitignored)
BASELINE="$STATE/baseline.json"
ALERT="$STATE/DRIFT-ALERT.txt"
LOG="$STATE/drift-check.log"
CE="$(command -v coord-engine || echo "$HOME/.local/bin/coord-engine")"
TEAM="fulcra"
AGENT="claude-code:Mac:fulcra-primitives-maintainer"
mkdir -p "$STATE"
ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "$(ts)  $*" >> "$LOG"; }

# --- capture current fingerprint ---
TMP="$(mktemp)"
SPEC="$(mktemp)"
curl -s --max-time 20 https://api.fulcradynamics.com/openapi.json -o "$SPEC"

fp="$(python3 - "$SPEC" <<'PY'
import sys, json, hashlib
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print("SPEC_FETCH_FAIL"); sys.exit(0)
paths = d.get("paths", {})
# annotation surface = the rewrite trigger; capture methods per annotation path
ann = {p: sorted(paths[p].keys()) for p in sorted(paths) if "annotation" in p}
ingest = {p: sorted(paths[p].keys()) for p in paths if p.startswith("/ingest/")}
files = sorted(p for p in paths if "file_upload" in p)  # usually none (not in spec)
sig = {
    "path_count": len(paths),
    "annotation_paths": ann,
    "ingest_paths": ingest,
    "has_DataRecordV1": "DataRecordV1" in d.get("components", {}).get("schemas", {}),
}
print(hashlib.sha256(json.dumps(sig, sort_keys=True).encode()).hexdigest()[:16] + " " + json.dumps(sig, sort_keys=True))
PY
)"
SPEC_HASH="${fp%% *}"; SPEC_SIG="${fp#* }"

CLI_HEAD="$(gh api repos/fulcradynamics/fulcra-api-python/commits/main --jq '.sha[:7]' 2>/dev/null || echo UNKNOWN)"
# annotation RECORD write/delete commands in CLI = the rewrite trigger
ANN_CMDS="$(gh api repos/fulcradynamics/fulcra-api-python/contents/fulcra_api/cli/data_types.py --jq '.content' 2>/dev/null | base64 -d 2>/dev/null | grep -E '@data_type\.command|@records?\.command' | wc -l | tr -d '[:space:]')"
MCP_SCOPES="$(curl -s --max-time 12 https://mcp.fulcradynamics.com/.well-known/oauth-authorization-server | python3 -c 'import sys,json;print(",".join(json.load(sys.stdin).get("scopes_supported",[])))' 2>/dev/null || echo UNKNOWN)"
# The CLI ships to PyPI AHEAD of git main (0.1.34 exposed the data-type/tag
# commands while main HEAD was unchanged), so a main-HEAD-only fingerprint
# misses real releases. Track the published version too — a version bump is the
# signal that a release may have changed the agent-facing surface.
PYPI_VER="$(curl -s --max-time 12 https://pypi.org/pypi/fulcra-api/json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null || echo UNKNOWN)"

CUR="$(python3 -c "import json,sys;print(json.dumps({'spec_hash':sys.argv[1],'cli_head':sys.argv[2],'ann_cmd_count':sys.argv[3],'mcp_scopes':sys.argv[4],'pypi_version':sys.argv[5]},sort_keys=True))" "$SPEC_HASH" "$CLI_HEAD" "$ANN_CMDS" "$MCP_SCOPES" "$PYPI_VER")"
echo "$CUR" > "$TMP"

# --- first run: write baseline, done ---
if [ ! -f "$BASELINE" ]; then
  cp "$TMP" "$BASELINE"
  log "baseline written: $CUR"
  rm -f "$SPEC" "$TMP"; exit 0
fi

PREV="$(cat "$BASELINE")"
if [ "$CUR" = "$PREV" ]; then
  log "no drift ($SPEC_HASH cli=$CLI_HEAD ann_cmds=$ANN_CMDS)"
  rm -f "$SPEC" "$TMP"; exit 0
fi

# --- drift! alert ---
{
  echo "DRIFT DETECTED $(ts)"
  echo "PREV: $PREV"
  echo "NOW:  $CUR"
  echo "spec_sig: $SPEC_SIG"
} > "$ALERT"
log "DRIFT: prev=$PREV now=$CUR"
"$CE" tell "$TEAM" "$AGENT" "DRIFT: Fulcra primitives surface changed — re-verify + rewrite FULCRA-PRIMITIVES.md" --from "$AGENT" --workstream fulcra-primitives --priority P2 --summary "Narrow drift vs baseline. PREV=$PREV NOW=$CUR. If ann_cmd_count rose or a record verb appeared, annotation RECORD commands may have landed = the documented full-rewrite trigger (tier-2 API-direct guidance shifts too). Alert file: $ALERT." >> "$LOG" 2>&1
# advance baseline so we alert once per change, not every day
cp "$TMP" "$BASELINE"
rm -f "$SPEC" "$TMP"
exit 0

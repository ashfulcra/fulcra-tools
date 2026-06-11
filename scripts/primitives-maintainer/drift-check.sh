#!/bin/bash
# fulcra-primitives-maintainer — daily drift detector.
# Fingerprints the live Fulcra surface (OpenAPI spec, CLI HEAD, annotation
# commands, MCP discovery) and compares to a stored baseline. On drift it
# posts to the fulcra-coord bus as fulcra-primitives-maintainer and writes an
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
COORD="$(command -v fulcra-coord || echo "$HOME/.local/bin/fulcra-coord")"
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
ANN_CMDS="$(gh api repos/fulcradynamics/fulcra-api-python/contents/fulcra_api/cli/data_types.py --jq '.content' 2>/dev/null | base64 -d 2>/dev/null | grep -coE '@data_type\.command|@records?\.command' || echo 0)"
MCP_SCOPES="$(curl -s --max-time 12 https://mcp.fulcradynamics.com/.well-known/oauth-authorization-server | python3 -c 'import sys,json;print(",".join(json.load(sys.stdin).get("scopes_supported",[])))' 2>/dev/null || echo UNKNOWN)"

CUR="$(python3 -c "import json,sys;print(json.dumps({'spec_hash':sys.argv[1],'cli_head':sys.argv[2],'ann_cmd_count':sys.argv[3],'mcp_scopes':sys.argv[4]},sort_keys=True))" "$SPEC_HASH" "$CLI_HEAD" "$ANN_CMDS" "$MCP_SCOPES")"
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
"$COORD" tell claude-code:Mac:fulcra-tools "DRIFT ALERT (fulcra-primitives-maintainer): the live Fulcra surface changed vs FULCRA-PRIMITIVES.md baseline. PREV=$PREV NOW=$CUR. Triggering a full re-verification + doc rewrite. If ann_cmd_count rose, annotation RECORD commands may have landed in the CLI = the documented full-rewrite trigger (tier-2 API-direct guidance shifts too). Alert at $ALERT. — claude-code:Mac:fulcra-primitives-maintainer" >> "$LOG" 2>&1
# advance baseline so we alert once per change, not every day
cp "$TMP" "$BASELINE"
rm -f "$SPEC" "$TMP"
exit 0

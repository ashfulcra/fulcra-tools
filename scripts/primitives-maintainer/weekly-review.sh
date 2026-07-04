#!/bin/bash
# fulcra-primitives-maintainer — WEEKLY full-re-read backstop.
# The daily drift check fingerprints a narrow surface (endpoint paths,
# annotation methods, CLI HEAD, MCP scopes). This weekly job casts a wider net
# to catch what the narrow check misses — docs PROSE changes, new MCP tools,
# newly-added endpoints/schemas — AND always drops a flag asking a Claude
# session to do a genuine human-eyes full re-read of FULCRA-PRIMITIVES.md
# against reality (model judgment the shell can't do).
set -uo pipefail

# ROOT = the maintainer checkout root (two levels up from scripts/primitives-maintainer/).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE="$ROOT/.primitives-state"        # local runtime state (gitignored)
WBASE="$STATE/weekly-baseline.json"
WFLAG="$STATE/WEEKLY-REVIEW-DUE.txt"
LOG="$STATE/weekly-review.log"
CE="$(command -v coord-engine || echo "$HOME/.local/bin/coord-engine")"
TEAM="fulcra"
AGENT="claude-code:Mac:fulcra-primitives-maintainer"
mkdir -p "$STATE"
ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "$(ts)  $*" >> "$LOG"; }

SPEC="$(mktemp)"
curl -s --max-time 20 https://api.fulcradynamics.com/openapi.json -o "$SPEC"

# wide fingerprint: full sorted path set + all method tuples + all schema names
WIDE_SPEC="$(python3 - "$SPEC" <<'PY'
import sys, json, hashlib
try: d = json.load(open(sys.argv[1]))
except Exception: print("FAIL"); sys.exit(0)
paths = d.get("paths", {})
methods = sorted(f"{p}:{m}" for p in paths for m in paths[p])
schemas = sorted(d.get("components", {}).get("schemas", {}).keys())
blob = json.dumps({"methods": methods, "schemas": schemas}, sort_keys=True)
print(hashlib.sha256(blob.encode()).hexdigest()[:16])
PY
)"

# docs prose + MCP surface (content hashes — catches text the narrow check ignores)
docs_hash() { curl -sL --max-time 15 "$1" | python3 -c "import sys,hashlib,re;t=sys.stdin.read();t=re.sub(r'<(script|style).*?</\1>','',t,flags=re.S);t=re.sub(r'<[^>]+>',' ',t);t=re.sub(r'\s+',' ',t);print(hashlib.sha256(t.strip().encode()).hexdigest()[:16])" 2>/dev/null || echo FAIL; }
DOCS_OVERVIEW="$(docs_hash https://docs.fulcradynamics.com/)"
DOCS_API="$(docs_hash https://docs.fulcradynamics.com/api-reference/)"
DOCS_MCP="$(docs_hash https://docs.fulcradynamics.com/mcp-server/)"
MCP_DISC="$(curl -s --max-time 12 https://mcp.fulcradynamics.com/.well-known/oauth-authorization-server | python3 -c 'import sys,json,hashlib;print(hashlib.sha256(json.dumps(json.load(sys.stdin),sort_keys=True).encode()).hexdigest()[:16])' 2>/dev/null || echo FAIL)"

CUR="$(python3 -c "import json,sys;print(json.dumps(dict(zip(['wide_spec','docs_overview','docs_api','docs_mcp','mcp_disc'],sys.argv[1:6])),sort_keys=True))" "$WIDE_SPEC" "$DOCS_OVERVIEW" "$DOCS_API" "$DOCS_MCP" "$MCP_DISC")"
rm -f "$SPEC"

# Always drop the human-eyes-review flag (the point of the weekly backstop).
{
  echo "WEEKLY FULL RE-READ DUE $(ts)"
  echo "A session should: re-read FULCRA-PRIMITIVES.md end-to-end vs the live"
  echo "API spec, CLI --help, docs.fulcradynamics.com pages, and the MCP server,"
  echo "fix any drift, bump the 'Verified against' header, push doc-only to main."
  echo "wide fingerprint NOW: $CUR"
} > "$WFLAG"

CHANGED=""
if [ -f "$WBASE" ]; then
  PREV="$(cat "$WBASE")"
  [ "$CUR" != "$PREV" ] && CHANGED="yes"
fi
echo "$CUR" > "$WBASE"

if [ -n "$CHANGED" ]; then
  log "WIDE DRIFT: $CUR (prev differed)"
  "$CE" tell "$TEAM" "$AGENT" "WEEKLY WIDE-DRIFT: full human-eyes re-read of FULCRA-PRIMITIVES.md needed" --from "$AGENT" --workstream fulcra-primitives --priority P2 --summary "Broad fingerprint changed — could be new endpoints/schemas, docs prose, or MCP surface the daily narrow check misses. NOW=$CUR." >> "$LOG" 2>&1
else
  log "weekly review flag dropped; no wide drift vs last week ($CUR)"
fi
exit 0

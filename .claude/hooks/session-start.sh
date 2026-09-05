#!/usr/bin/env bash
# .claude/hooks/session-start.sh — make a fresh container usable without a human.
#
# Two things have to be true before any coord work can happen on this repo, and
# on 2026-09-05 both stopped being true inside a long-lived container, silently:
#
#   1. ~/.config/fulcra/credentials.json must exist. The fulcra-api CLI reads
#      credentials ONLY from that file. Its source honours FULCRA_OIDC_DOMAIN,
#      FULCRA_OIDC_CLIENT_ID, FULCRA_OIDC_AUDIENCE and FULCRA_OIDC_SCOPE, but
#      there is NO env-var token path — so a container without that file has no
#      CLI auth at all and every store verb fails "No credentials found".
#      Three scheduled mesh sweeps failed that way before anyone noticed.
#
#   2. .venv must exist, or nothing in packages/ can be imported or tested.
#
# Both legs are idempotent and both are no-ops on a healthy box. Neither ever
# prints a credential value.
#
# NOTE the MCP Fulcra connector is unaffected by leg 1 — it authenticates
# through the account, not this file. Store reads and writes remain available
# through it even when this hook reports NO CREDENTIALS.
set -uo pipefail

say() { printf 'session-start: %s\n' "$*" >&2; }
loud() {
  printf '\n' >&2
  printf 'session-start: ============================================================\n' >&2
  printf 'session-start: %s\n' "$@" >&2
  printf 'session-start: ============================================================\n\n' >&2
}

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

# --------------------------------------------------------------------------
# Leg 1: Fulcra CLI credentials
# --------------------------------------------------------------------------
cred_dir="${HOME}/.config/fulcra"
cred_file="${cred_dir}/credentials.json"

if [ -s "$cred_file" ]; then
  say "fulcra credentials present"
elif [ -n "${FULCRA_CREDENTIALS_JSON:-}" ]; then
  mkdir -p "$cred_dir" && chmod 700 "$cred_dir"
  tmp="${cred_file}.tmp.$$"
  ( umask 077; printf '%s' "${FULCRA_CREDENTIALS_JSON}" > "$tmp" )
  # Validate the SHAPE before installing it. Never echo the value; on failure
  # report field NAMES only, so a malformed secret is diagnosable without
  # leaking one. A bad secret must not replace a good file.
  if python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));assert isinstance(d,dict),"not a JSON object";missing=sorted({"access_token","refresh_token"}-set(d));assert not missing,"missing fields: %s (present: %s)"%(missing,sorted(d))' "$tmp" 2>/tmp/fulcra-cred-check.$$; then
    mv "$tmp" "$cred_file" && chmod 600 "$cred_file"
    say "fulcra credentials restored from FULCRA_CREDENTIALS_JSON"
  else
    rm -f "$tmp"
    loud "FULCRA_CREDENTIALS_JSON is set but is NOT a usable credentials document." \
         "$(sed -n '$p' /tmp/fulcra-cred-check.$$ 2>/dev/null | tail -c 200)" \
         "Nothing was installed. The CLI has no auth; the MCP connector still does."
  fi
  rm -f /tmp/fulcra-cred-check.$$
else
  loud "NO FULCRA CLI CREDENTIALS, and FULCRA_CREDENTIALS_JSON is not set." \
       "Every 'fulcra-api' and 'coord-engine' store verb will fail with" \
       "\"No credentials found\". This is NOT a store outage." \
       "" \
       "Fix, in order of durability:" \
       "  1. Set FULCRA_CREDENTIALS_JSON in the environment config (survives" \
       "     every container). Ask the operator; never mint or share a token" \
       "     between agents yourself." \
       "  2. One-off: fulcra-api auth login --get-auth-url, give the URL to the" \
       "     operator, then fulcra-api auth login --device-code <code>." \
       "" \
       "The MCP Fulcra connector is unaffected and can still read/write the store."
fi

# --------------------------------------------------------------------------
# Leg 2: workspace venv
# --------------------------------------------------------------------------
if [ -x "${PROJECT_DIR}/.venv/bin/python" ]; then
  say "venv present"
elif command -v uv >/dev/null 2>&1; then
  # The sync recipe is OS-dependent, and getting this wrong is how the hook
  # first failed here. AGENTS.md documents "uv sync --all-packages --all-extras"
  # as THE manual equivalent, but that is the macOS recipe (.github/workflows/
  # macos.yml): --all-extras pulls the `macos` extra, whose PyObjC wheels build
  # by shelling out to /usr/bin/sw_vers, which does not exist on Linux. The
  # build dies with FileNotFoundError and NOTHING gets installed.
  #
  # The Linux workflows (uv-workspace.yml, coord-fold-proof.yml) use a plain
  # --all-packages sync and then select the dev extra at RUN time:
  #     uv run --all-packages --extra dev python -m pytest packages/ -q
  # so that is what a non-Darwin box gets here.
  if [ "$(uname -s)" = "Darwin" ]; then
    sync_args=(--all-packages --all-extras)
  else
    sync_args=(--all-packages)
  fi
  say "no .venv — running uv sync ${sync_args[*]} (this takes a minute)"
  if (cd "$PROJECT_DIR" && uv sync "${sync_args[@]}" >/tmp/uv-sync.$$ 2>&1); then
    say "venv built"
  else
    loud "uv sync FAILED — packages/ cannot be imported or tested." \
         "$(tail -n 5 /tmp/uv-sync.$$ 2>/dev/null)"
  fi
  rm -f /tmp/uv-sync.$$
else
  loud "no .venv and no uv on PATH — packages/ cannot be imported or tested."
fi

exit 0

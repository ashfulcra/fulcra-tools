#!/usr/bin/env bash
# .claude/hooks/session-start.sh — make a fresh container able to work.
#
# This repo is PUBLIC. Nothing team-specific belongs in this file: it carries
# the MECHANISM, and any particular team's configuration is fetched from that
# team's own store at run time (leg 3). Adding a team's paths, identities or
# operational history here is the thing this layout exists to prevent.
#
# Three legs, each idempotent, each a no-op on a healthy box, none of which
# ever prints a credential value:
#
#   1. workspace venv        — without it nothing in packages/ imports
#   2. Fulcra CLI credentials — restored from the environment when provided
#   3. team configuration     — fetched from the store, never baked in here
#
# The hook always exits 0. A bootstrap problem should make the session loud,
# not unusable.
set -uo pipefail

say()  { printf 'session-start: %s\n' "$*" >&2; }
loud() {
  printf '\n' >&2
  printf 'session-start: ============================================================\n' >&2
  printf 'session-start: %s\n' "$@" >&2
  printf 'session-start: ============================================================\n\n' >&2
}

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

# ---------------------------------------------------------------------------
# Leg 1: workspace venv
#
# The sync recipe is OS-dependent. AGENTS.md gives
# `uv sync --all-packages --all-extras` as the manual equivalent, but that is
# the macOS recipe (.github/workflows/macos.yml): --all-extras pulls the
# `macos` extra, whose PyObjC wheels build by shelling out to /usr/bin/sw_vers,
# which does not exist on Linux. The build dies with FileNotFoundError and
# NOTHING is installed — the failure leaves a .venv containing only `python`,
# which looks like success to a casual check. The Linux workflows
# (uv-workspace.yml, coord-fold-proof.yml) sync plain --all-packages and select
# the dev extra at RUN time:
#     uv run --all-packages --extra dev python -m pytest packages/ -q
# ---------------------------------------------------------------------------
if [ -x "${PROJECT_DIR}/.venv/bin/python" ]; then
  say "venv present"
elif command -v uv >/dev/null 2>&1; then
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

# ---------------------------------------------------------------------------
# Leg 2: Fulcra CLI credentials
#
# The fulcra-api CLI reads credentials ONLY from ~/.config/fulcra/credentials.json.
# Its source honours FULCRA_OIDC_DOMAIN, _CLIENT_ID, _AUDIENCE and _SCOPE, but
# there is NO env-var token path — so a container that starts without that file
# has no CLI auth at all and every store verb fails "No credentials found",
# which reads exactly like a store outage and is not one.
#
# Set FULCRA_CREDENTIALS_JSON in the environment to make this survive a
# container. Note the trade before you do: an environment variable is readable
# by every session and agent in that environment.
# ---------------------------------------------------------------------------
cred_dir="${HOME}/.config/fulcra"
cred_file="${cred_dir}/credentials.json"
have_creds=0

if [ -s "$cred_file" ]; then
  say "fulcra credentials present"
  chmod 700 "$cred_dir" 2>/dev/null
  # The CLI writes this file 0644. It holds a refresh token; tighten it.
  chmod 600 "$cred_file" 2>/dev/null
  have_creds=1
elif [ -n "${FULCRA_CREDENTIALS_JSON:-}" ]; then
  mkdir -p "$cred_dir" && chmod 700 "$cred_dir"
  tmp="${cred_file}.tmp.$$"
  ( umask 077; printf '%s' "${FULCRA_CREDENTIALS_JSON}" > "$tmp" )
  # Validate SHAPE before installing. Never echo the value; on failure report
  # field NAMES only, so a malformed secret is diagnosable without leaking one.
  # Writing via a temp file means a bad secret can never replace a good file.
  if python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));assert isinstance(d,dict),"not a JSON object";missing=sorted({"access_token","refresh_token"}-set(d));assert not missing,"missing fields: %s (present: %s)"%(missing,sorted(d))' "$tmp" 2>/tmp/cred-check.$$; then
    mv "$tmp" "$cred_file" && chmod 600 "$cred_file"
    say "fulcra credentials restored from FULCRA_CREDENTIALS_JSON"
    have_creds=1
  else
    rm -f "$tmp"
    loud "FULCRA_CREDENTIALS_JSON is set but is NOT a usable credentials document." \
         "$(tail -n 1 /tmp/cred-check.$$ 2>/dev/null | tail -c 200)" \
         "Nothing was installed."
  fi
  rm -f /tmp/cred-check.$$
else
  loud "NO FULCRA CLI CREDENTIALS, and FULCRA_CREDENTIALS_JSON is not set." \
       "Every 'fulcra-api' store verb will fail with \"No credentials found\"." \
       "This is NOT a store outage." \
       "" \
       "  durable : set FULCRA_CREDENTIALS_JSON in the environment config" \
       "  one-off : fulcra-api auth login --get-auth-url, give the URL to the" \
       "            operator, then fulcra-api auth login --device-code <code>" \
       "" \
       "Never mint or share a token between agents yourself." \
       "An MCP Fulcra connector, if this session has one, is unaffected: it" \
       "authenticates through the account rather than this file."
fi

# ---------------------------------------------------------------------------
# Leg 3: team configuration — fetched, never baked in
#
# This repo is public and generic; a team's own setup is its own business and
# lives on its own store. When FULCRA_COORD_TEAM names a team and the CLI can
# reach the store, the hook reads:
#
#     /team/<team>/_coord/bus-v3/session-config.json
#
# It is CONFIG, not code: the hook never executes anything it downloads. The
# schema is deliberately tiny, and unknown keys are ignored so the bus side can
# grow without every box needing a new hook:
#
#     {
#       "notes":  ["free text printed at session start"],
#       "checks": [ {"name": "...", "path": "/abs/path"} ]
#     }
#
# `checks` are existence checks on local paths, reported and never repaired —
# a hook that silently fixes things teaches you to stop reading it.
# An absent config is normal and silent: most boxes have nothing to say here.
# ---------------------------------------------------------------------------
team="${FULCRA_COORD_TEAM:-}"
if [ -n "$team" ] && [ "$have_creds" = "1" ] && command -v fulcra-api >/dev/null 2>&1; then
  cfg="/tmp/session-config.$$.json"
  if fulcra-api file download "/team/${team}/_coord/bus-v3/session-config.json" "$cfg" >/dev/null 2>&1; then
    python3 - "$cfg" <<'PYEOF' >&2
import json, os, sys
try:
    cfg = json.load(open(sys.argv[1]))
except Exception as e:
    print("session-start: team config is present but unreadable (%s); ignoring"
          % type(e).__name__)
    raise SystemExit(0)
for note in (cfg.get("notes") or []):
    print("session-start: [team] %s" % note)
missing = [c for c in (cfg.get("checks") or [])
           if isinstance(c, dict) and c.get("path")
           and not os.path.exists(os.path.expanduser(c["path"]))]
for c in missing:
    print("session-start: [team] MISSING: %s (%s)"
          % (c.get("name") or c["path"], c["path"]))
if missing:
    print("session-start: [team] %d declared path(s) absent — reported, not repaired"
          % len(missing))
PYEOF
  fi
  rm -f "$cfg"
fi

exit 0

#!/usr/bin/env bash
# install-heartbeat.sh — schedule `coord-engine reconcile <team>` on a timer so a
# fulcra-agent-teams space's index/views stay healed without a human running it.
#
# Usage:
#   install-heartbeat.sh [--yes] <team> [interval-minutes]   # default 20
#   install-heartbeat.sh --uninstall <team>
#
# macOS -> a launchd LaunchAgent; Linux -> a crontab line. Idempotent. Requires
# `coord-engine` and `fulcra-api` (authenticated) on PATH at install time.
set -euo pipefail

YES=0; UNINSTALL=0
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --yes) YES=1;;
    --uninstall) UNINSTALL=1;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
  shift
done

TEAM="${1:?usage: install-heartbeat.sh [--yes] <team> [interval-minutes] | --uninstall <team>}"
INTERVAL="${2:-20}"

# B1/B2 — validate inputs before they reach XML / a path / a cron line / a grep pattern.
[[ "$TEAM" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "error: team must match [A-Za-z0-9_-]+ (got: '$TEAM')" >&2; exit 2; }
[[ "$INTERVAL" =~ ^[0-9]+$ ]] && (( INTERVAL >= 1 )) || { echo "error: interval must be a positive integer (minutes)" >&2; exit 2; }

LABEL="com.fulcra.coord-engine.heartbeat.${TEAM}"

# Resolve tool paths at install time; the scheduled job runs with a minimal PATH
# (launchd/cron source no profile), so we pin an explicit PATH that includes the
# dirs of BOTH coord-engine and the fulcra-api it shells out to (the parent
# project's heartbeats silently failed exactly here). Auth lives in
# ~/.config/fulcra/credentials.json, which fulcra-api finds via $HOME (set below).
CE="$(command -v coord-engine || true)"
FA="$(command -v fulcra-api || true)"
if [[ "$UNINSTALL" != "1" ]]; then
  [[ -n "$CE" ]] || { echo "error: coord-engine not found on PATH" >&2; exit 3; }
  [[ -n "$FA" ]] || { echo "error: fulcra-api not found on PATH (coord-engine needs it)" >&2; exit 3; }
fi
JOB_PATH="$(dirname "${CE:-/usr/bin/true}"):$(dirname "${FA:-/usr/bin/true}"):/usr/local/bin:/usr/bin:/bin"
CEB="${CE:-coord-engine}"
# Reconcile heals the views AND (when the team opted in via `annotate resolution
# <team> transitions`) writes the pass's structured transitions; `annotate
# project` then folds them onto the operator's Fulcra timeline right after. The
# projection self-gates on the bus resolution level, so this runs unconditionally
# and is a cheap exit-0 no-op when projection is off — one degradation-safe chain.
# The digest leg keeps the operator's twice-daily digest alive on BOTH surfaces
# (bus copy + 'Agent Tasks — Digest' timeline track). Every tick may run it:
# the timeline record id is DETERMINISTIC per (team, day, window) and the
# ingest endpoint upserts on explicit ids, so concurrent hosts and retries
# converge on one record; a failed emit retries on the next tick; a missing
# fulcra_common writer warns loud but never breaks the chain (rc 0).
CMD="${CEB} reconcile ${TEAM} && ${CEB} annotate project ${TEAM} && ${CEB} digest ${TEAM} --store --emit-timeline"

confirm() {
  [[ "$YES" == "1" ]] && return 0
  read -r -p "$1 [y/N] " ans || true
  [[ "$ans" == "y" || "$ans" == "Y" ]]
}

selftest() {  # B3 — run once at install so a broken PATH/auth fails LOUDLY now, not silently in 20m
  echo "self-test: running the heartbeat once (PATH=$JOB_PATH)…"
  if env -i HOME="$HOME" PATH="$JOB_PATH" /bin/sh -c "$CMD" >/dev/null 2>&1; then
    echo "self-test: OK (chain exit 0)"
  else
    echo "WARNING: self-test failed — the scheduled job would silently fail. Verify" >&2
    echo "  fulcra-api is authenticated (fulcra-api auth login) and reachable under PATH=$JOB_PATH" >&2
  fi
  # Exit 0 is NOT proof the timeline legs can land: projection and the digest
  # emit deliberately degrade to loud-warn no-ops when the fulcra_common writer
  # is absent (the chain must never break), which is exactly how a timeline
  # goes dark while every tick reports success. The installed chain requests
  # timeline emission UNCONDITIONALLY (`digest --emit-timeline` runs every
  # tick regardless of the team's projection resolution), so the writer check
  # is unconditional too — gating it on `resolution=transitions` would pass
  # the exact digest-only dark-timeline condition (codex docs-QA P1, twice).
  local cebin cepy
  cebin="$(command -v "$CEB" || echo "$CEB")"
  cepy="$(dirname "$(readlink -f "$cebin")")/python"
  if [[ -x "$cepy" ]] && "$cepy" -c "import fulcra_common" >/dev/null 2>&1; then
    echo "self-test: timeline writer present (fulcra_common importable next to coord-engine)"
  else
    echo "ERROR: self-test FAILED — the heartbeat chain emits timeline moments" >&2
    echo "  (digest --emit-timeline every tick; annotate project when the team opts in)" >&2
    echo "  but the fulcra_common writer is NOT importable in coord-engine's" >&2
    echo "  environment: those emits will no-op and the timeline stays dark." >&2
    echo "  Reinstall with the writer:" >&2
    echo "    uv tool install --force \"git+https://github.com/ashfulcra/fulcra-tools@coord-engine-v1.6.9#subdirectory=packages/coord-engine\" \\" >&2
    echo "      --with \"git+https://github.com/ashfulcra/fulcra-tools@fulcra-common-v0.2.0#subdirectory=packages/fulcra-common\"" >&2
    exit 4
  fi
}

os="$(uname -s)"
if [[ "$os" == "Darwin" ]]; then
  PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
  LOGDIR="$HOME/Library/Logs/coord-engine"; mkdir -p "$LOGDIR"
  if [[ "$UNINSTALL" == "1" ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true; rm -f "$PLIST"
    echo "uninstalled launchd heartbeat for team/${TEAM}"; exit 0
  fi
  confirm "Install a launchd heartbeat running '${CMD}' every ${INTERVAL}m?" || { echo "aborted."; exit 0; }
  launchctl unload "$PLIST" 2>/dev/null || true
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>${JOB_PATH}</string>
    <key>HOME</key><string>${HOME}</string>
  </dict>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-c</string><string>${CMD//&/&amp;}</string>
  </array>
  <key>StartInterval</key><integer>$(( INTERVAL * 60 ))</integer>
  <key>StandardErrorPath</key><string>${LOGDIR}/heartbeat-${TEAM}.err.log</string>
  <key>StandardOutPath</key><string>${LOGDIR}/heartbeat-${TEAM}.out.log</string>
</dict></plist>
PLIST
  if command -v plutil >/dev/null 2>&1 && ! plutil -lint "$PLIST" >/dev/null 2>&1; then
    echo "error: generated plist failed plutil -lint; not loading" >&2; rm -f "$PLIST"; exit 4
  fi
  launchctl load "$PLIST"
  echo "installed launchd heartbeat: team/${TEAM} every ${INTERVAL}m ($PLIST)"
  selftest
else
  LOGDIR="$HOME/.cache/coord-engine"; mkdir -p "$LOGDIR"
  # PATH= prefix so the cron job finds coord-engine + fulcra-api (cron's PATH is minimal).
  LINE="*/${INTERVAL} * * * * PATH=${JOB_PATH} ${CMD} >> ${LOGDIR}/heartbeat-${TEAM}.log 2>&1  # ${LABEL}"
  current="$(crontab -l 2>/dev/null || true)"
  # drop our own prior line by exact label comment (fixed string via grep -F, not a regex over $TEAM)
  filtered="$(printf '%s\n' "$current" | grep -vF "# ${LABEL}" || true)"
  if [[ "$UNINSTALL" == "1" ]]; then
    if [[ -n "$filtered" ]]; then printf '%s\n' "$filtered" | crontab -
    else crontab - </dev/null   # our line was the only entry: install an empty crontab (pipefail-safe)
    fi
    echo "uninstalled cron heartbeat for team/${TEAM}"; exit 0
  fi
  confirm "Install a cron heartbeat running '${CMD}' every ${INTERVAL}m?" || { echo "aborted."; exit 0; }
  if [[ -n "$filtered" ]]; then printf '%s\n%s\n' "$filtered" "$LINE" | crontab -
    else printf '%s\n' "$LINE" | crontab -
    fi
  echo "installed cron heartbeat: team/${TEAM} every ${INTERVAL}m"
  selftest
fi

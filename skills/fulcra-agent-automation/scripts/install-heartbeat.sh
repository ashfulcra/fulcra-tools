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
CMD="${CE:-coord-engine} reconcile ${TEAM}"

confirm() {
  [[ "$YES" == "1" ]] && return 0
  read -r -p "$1 [y/N] " ans || true
  [[ "$ans" == "y" || "$ans" == "Y" ]]
}

selftest() {  # B3 — run once at install so a broken PATH/auth fails LOUDLY now, not silently in 20m
  echo "self-test: running the heartbeat once (PATH=$JOB_PATH)…"
  if env -i HOME="$HOME" PATH="$JOB_PATH" /bin/sh -c "$CMD" >/dev/null 2>&1; then
    echo "self-test: OK"
  else
    echo "WARNING: self-test failed — the scheduled job would silently fail. Verify" >&2
    echo "  fulcra-api is authenticated (fulcra-api auth login) and reachable under PATH=$JOB_PATH" >&2
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
    <string>${CE}</string><string>reconcile</string><string>${TEAM}</string>
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

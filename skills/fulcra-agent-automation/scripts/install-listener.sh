#!/usr/bin/env bash
# install-listener.sh — schedule listener-tick.sh (inbox check + notification +
# optional consent-gated wake) for an agent. Mirrors install-heartbeat.sh's
# hardening: validated inputs, pinned PATH/HOME, plutil lint, install self-test.
#
# Usage:
#   install-listener.sh [--yes] <team> <agent> [active-minutes]
#       [--tail-minutes N] [--idle-minutes N] [--fixed] [--wake-cmd "cmd..."]
#   install-listener.sh --uninstall <team> <agent>
set -euo pipefail

YES=0; UNINSTALL=0; WAKE_CMD=""; ADAPTIVE=1; TAIL_MINUTES=30; IDLE_MINUTES=30
POS=()
usage() {
  cat <<'EOF'
usage:
  install-listener.sh [--yes] <team> <agent> [interval-minutes] [--wake-cmd "cmd..."]
  install-listener.sh --uninstall <team> <agent>

Schedules listener-tick.sh (inbox check + notification + optional consent-gated
wake) for an agent. --yes skips the consent prompt; --wake-cmd must be one line
with no single quotes or angle brackets.
EOF
}
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0;;
    --yes) YES=1;;
    --uninstall) UNINSTALL=1;;
    --fixed) ADAPTIVE=0;;
    --tail-minutes) TAIL_MINUTES="${2:?--tail-minutes needs an integer}"; shift;;
    --idle-minutes) IDLE_MINUTES="${2:?--idle-minutes needs an integer}"; shift;;
    --wake-cmd) WAKE_CMD="${2:?--wake-cmd needs a command}"
                case "$WAKE_CMD" in *$'\n'*|*$'\r'*)
                  echo "error: --wake-cmd must be one line" >&2; exit 2;;
                esac
                case "$WAKE_CMD" in *"'"*|*"<"*|*">"*)
                  echo "error: --wake-cmd may not contain single quotes or angle brackets" >&2; exit 2;;
                esac; shift;;
    --*) echo "unknown option: $1" >&2; exit 2;;
    *) POS+=("$1");;
  esac
  shift
done
TEAM="${POS[0]:?usage: install-listener.sh [--yes] <team> <agent> [interval-min] [--wake-cmd ...]}"
AGENT="${POS[1]:?usage: install-listener.sh [--yes] <team> <agent> [interval-min] [--wake-cmd ...]}"
INTERVAL="${POS[2]:-1}"

# validation (the L7-review lesson: inputs reach XML, paths, cron lines)
[[ "$TEAM" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "error: team must match [A-Za-z0-9_-]+" >&2; exit 2; }
[[ "$AGENT" =~ ^[A-Za-z0-9:_.-]+$ ]] || { echo "error: agent must match [A-Za-z0-9:_.-]+" >&2; exit 2; }
[[ "$INTERVAL" =~ ^[0-9]+$ ]] && (( INTERVAL >= 1 )) || { echo "error: interval must be a positive integer" >&2; exit 2; }
[[ "$TAIL_MINUTES" =~ ^[0-9]+$ ]] || { echo "error: --tail-minutes must be a non-negative integer" >&2; exit 2; }
[[ "$IDLE_MINUTES" =~ ^[0-9]+$ ]] && (( IDLE_MINUTES >= INTERVAL )) || {
  echo "error: --idle-minutes must be an integer >= active interval" >&2; exit 2; }

SAFE_AGENT="$(printf '%s' "$AGENT" | tr -c 'A-Za-z0-9_.-' '-')-$(printf '%s' "$AGENT" | cksum | cut -d' ' -f1)"
LABEL="com.fulcra.coord-engine.listener.${TEAM}.${SAFE_AGENT}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TICK="$SCRIPT_DIR/listener-tick.sh"

CE="$(command -v coord-engine || true)"
FA="$(command -v fulcra-api || true)"
if [[ "$UNINSTALL" != "1" ]]; then
  [[ -n "$CE" ]] || { echo "error: coord-engine not found on PATH" >&2; exit 3; }
  [[ -n "$FA" ]] || { echo "error: fulcra-api not found on PATH (the inbox fold needs it)" >&2; exit 3; }
  [[ -x "$TICK" ]] || { echo "error: listener-tick.sh not found/executable next to installer" >&2; exit 3; }
  if [[ -n "$WAKE_CMD" && "$YES" != "1" ]]; then
    read -r -p "Wake command will run UNATTENDED on new inbox items: '$WAKE_CMD' — allow? [y/N] " a || true
    [[ "$a" == "y" || "$a" == "Y" ]] || { echo "aborted."; exit 0; }
  fi
fi
JOB_PATH="$(dirname "${CE:-/usr/bin/true}"):$(dirname "${FA:-/usr/bin/true}"):/usr/local/bin:/usr/bin:/bin"

confirm() { [[ "$YES" == "1" ]] && return 0; read -r -p "$1 [y/N] " a || true; [[ "$a" == "y" || "$a" == "Y" ]]; }

os="$(uname -s)"
if [[ "$os" == "Darwin" ]]; then
  PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
  LOGDIR="$HOME/Library/Logs/coord-engine"; mkdir -p "$LOGDIR"
  if [[ "$UNINSTALL" == "1" ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true; rm -f "$PLIST"
    echo "uninstalled listener for ${AGENT}/${TEAM}"; exit 0
  fi
  if [[ "$ADAPTIVE" == "1" ]]; then
    CADENCE_DESC="every ${INTERVAL}m while active/for ${TAIL_MINUTES}m after, then every ${IDLE_MINUTES}m"
  else
    CADENCE_DESC="every ${INTERVAL}m (fixed)"
  fi
  confirm "Install a launchd listener checking ${AGENT}'s inbox in team/${TEAM} ${CADENCE_DESC}?" \
    || { echo "aborted."; exit 0; }
  launchctl unload "$PLIST" 2>/dev/null || true
  ARGS="<string>${TICK}</string>"
  if [[ "$ADAPTIVE" == "1" ]]; then
    ARGS="${ARGS}<string>--adaptive</string><string>--active-minutes</string><string>${INTERVAL}</string><string>--tail-minutes</string><string>${TAIL_MINUTES}</string><string>--idle-minutes</string><string>${IDLE_MINUTES}</string>"
  fi
  ARGS="${ARGS}<string>${TEAM}</string><string>${AGENT}</string>"
  if [[ -n "$WAKE_CMD" ]]; then
    ARGS="${ARGS}<string>/bin/sh</string><string>-c</string><string>${WAKE_CMD//&/&amp;}</string>"
  fi
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>${JOB_PATH}</string>
    <key>HOME</key><string>${HOME}</string>
  </dict>
  <key>ProgramArguments</key><array>${ARGS}</array>
  <key>StartInterval</key><integer>$(( INTERVAL * 60 ))</integer>
  <key>StandardErrorPath</key><string>${LOGDIR}/listener-${TEAM}-${SAFE_AGENT}.err.log</string>
  <key>StandardOutPath</key><string>${LOGDIR}/listener-${TEAM}-${SAFE_AGENT}.out.log</string>
</dict></plist>
PLIST
  if command -v plutil >/dev/null 2>&1 && ! plutil -lint "$PLIST" >/dev/null 2>&1; then
    echo "error: generated plist failed lint; not loading" >&2; rm -f "$PLIST"; exit 4
  fi
  launchctl load "$PLIST"
  echo "installed listener: ${AGENT}/${TEAM} ${CADENCE_DESC} ($PLIST)"
else
  if [[ "$UNINSTALL" == "1" ]]; then
    current="$(crontab -l 2>/dev/null || true)"
    filtered="$(printf '%s\n' "$current" | grep -vF "# ${LABEL}" || true)"
    if [[ -n "$filtered" ]]; then printf '%s\n' "$filtered" | crontab -
    else crontab - </dev/null   # our line was the only entry: install an empty crontab (pipefail-safe)
    fi
    echo "uninstalled listener for ${AGENT}/${TEAM}"; exit 0
  fi
  if [[ "$ADAPTIVE" == "1" ]]; then
    CADENCE_DESC="every ${INTERVAL}m while active/for ${TAIL_MINUTES}m after, then every ${IDLE_MINUTES}m"
  else
    CADENCE_DESC="every ${INTERVAL}m (fixed)"
  fi
  confirm "Install a cron listener for ${AGENT}/${TEAM} ${CADENCE_DESC}?" || { echo "aborted."; exit 0; }
  LOGDIR="$HOME/.cache/coord-engine"; mkdir -p "$LOGDIR"
  TICK_ARGS=""
  if [[ "$ADAPTIVE" == "1" ]]; then
    TICK_ARGS="--adaptive --active-minutes ${INTERVAL} --tail-minutes ${TAIL_MINUTES} --idle-minutes ${IDLE_MINUTES}"
  fi
  LINE="*/${INTERVAL} * * * * PATH=${JOB_PATH} ${TICK} ${TICK_ARGS} ${TEAM} ${AGENT}"
  [[ -n "$WAKE_CMD" ]] && LINE="${LINE} /bin/sh -c '${WAKE_CMD}'"
  LINE="${LINE} >> ${LOGDIR}/listener-${TEAM}-${SAFE_AGENT}.log 2>&1  # ${LABEL}"
  current="$(crontab -l 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$current" | grep -vF "# ${LABEL}" || true)"
  if [[ -n "$filtered" ]]; then printf '%s\n%s\n' "$filtered" "$LINE" | crontab -
    else printf '%s\n' "$LINE" | crontab -
    fi
  echo "installed cron listener: ${AGENT}/${TEAM} ${CADENCE_DESC}"
fi

echo "self-test: one tick now…"
if [[ "$ADAPTIVE" == "1" ]]; then
  SELFTEST=(env -i HOME="$HOME" PATH="$JOB_PATH" COORD_LISTENER_FORCE=1
    COORD_LISTENER_MARK_ACTIVE=1 "$TICK"
    --adaptive --active-minutes "$INTERVAL" --tail-minutes "$TAIL_MINUTES"
    --idle-minutes "$IDLE_MINUTES" "$TEAM" "$AGENT")
else
  SELFTEST=(env -i HOME="$HOME" PATH="$JOB_PATH" COORD_LISTENER_FORCE=1 "$TICK" "$TEAM" "$AGENT")
fi
if "${SELFTEST[@]}" >/dev/null 2>&1; then
  echo "self-test: OK"
else
  echo "WARNING: self-test failed — check coord-engine/fulcra-api auth under PATH=$JOB_PATH" >&2
fi

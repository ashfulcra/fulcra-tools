#!/usr/bin/env bash
# listener-tick.sh — one scheduled inbox check for an agent. Invoked by the job
# install-listener.sh creates (or manually). On NEW items since the last tick:
# a macOS notification (osascript) or a log line, and optionally a consent-gated
# wake command. Exit 0 always (a tick never fails the schedule).
#
# Usage: listener-tick.sh <team> <agent> [wake-cmd...]
set -euo pipefail

TEAM="${1:?usage: listener-tick.sh <team> <agent> [wake-cmd...]}"
AGENT="${2:?usage: listener-tick.sh <team> <agent> [wake-cmd...]}"
shift 2 || true

STATE_DIR="${COORD_LISTENER_STATE:-$HOME/.cache/coord-engine}"
mkdir -p "$STATE_DIR"
SAFE_KEY="$(printf '%s-%s' "$TEAM" "$AGENT" | tr -c 'A-Za-z0-9_.-' '-')-$(printf '%s-%s' "$TEAM" "$AGENT" | cksum | cut -d' ' -f1)"
ITEMS_FILE="$STATE_DIR/listener-$SAFE_KEY.items"

ITEMS="$(coord-engine inbox "$TEAM" --agent "$AGENT" --json 2>/dev/null || echo '[]')"
# Track directive IDs, not just counts: an ack and a new directive can keep the
# same open count, but the new ID still deserves a notification.
CURRENT_KEYS="$(printf '%s' "$ITEMS" \
  | grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' \
  | sed 's/.*"name"[[:space:]]*:[[:space:]]*"//; s/"$//' \
  | sort -u || true)"
PREV_KEYS="$(cat "$ITEMS_FILE" 2>/dev/null || true)"
COUNT="$(printf '%s\n' "$CURRENT_KEYS" | sed '/^$/d' | wc -l | tr -d '[:space:]')"
NEW="$(comm -13 \
  <(printf '%s\n' "$PREV_KEYS" | sed '/^$/d' | sort -u) \
  <(printf '%s\n' "$CURRENT_KEYS" | sed '/^$/d' | sort -u) | wc -l | tr -d '[:space:]')"
printf '%s\n' "$CURRENT_KEYS" > "$ITEMS_FILE"

if [[ "$NEW" -gt 0 ]]; then
  MSG="coord: ${NEW} new directive(s) for ${AGENT} in team/${TEAM} (${COUNT} open)"
  echo "$(date -u +%FT%TZ) $MSG"
  if command -v osascript >/dev/null 2>&1; then
    # display only; TEAM/AGENT are validated by the installer, but escape quotes anyway
    SAFE_MSG="${MSG//\"/}"
    osascript -e "display notification \"${SAFE_MSG}\" with title \"coord inbox\"" || true
  fi
  if [[ "$#" -gt 0 ]]; then
    # consent-gated wake command (installer requires explicit --wake-cmd)
    "$@" || echo "$(date -u +%FT%TZ) wake command failed (exit $?)" >&2
  fi
else
  echo "$(date -u +%FT%TZ) no new items (${COUNT} open) for ${AGENT}/${TEAM}"
fi
exit 0

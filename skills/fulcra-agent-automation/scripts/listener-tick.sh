#!/usr/bin/env bash
# listener-tick.sh — one scheduled coord check for an agent, delegating to the
# engine's `listen` verb (the SINGLE implementation of the diff/notify logic; the
# tick used to hand-roll an `inbox --json` id-diff). Invoked by the job
# install-listener.sh creates (or manually). On NEW events since the last tick —
# new inbox directives OR responses to directives this agent owns — a macOS
# notification (osascript) or a log line, and optionally a consent-gated wake
# command. Exit 0 always (a tick never fails the schedule).
#
# Usage: listener-tick.sh <team> <agent> [wake-cmd...]
set -euo pipefail

TEAM="${1:?usage: listener-tick.sh <team> <agent> [wake-cmd...]}"
AGENT="${2:?usage: listener-tick.sh <team> <agent> [wake-cmd...]}"
shift 2 || true

STATE_DIR="${COORD_LISTENER_STATE:-$HOME/.cache/coord-engine}"
mkdir -p "$STATE_DIR"

# The engine owns the state/diff logic now; the tick is a thin delegate. Resolve
# coord-engine the same way as before: bare, from the job's pinned PATH. Ask the
# engine for its state-file path (slugify/agent_key naming lives there, not here).
STATE_FILE="$(coord-engine listen "$TEAM" --agent "$AGENT" --state-path 2>/dev/null || true)"

# One-time migration: earlier ticks tracked seen inbox ids in a `.items` file. If
# the engine's listen-state does not exist yet but that file does, seed inbox_ids
# from it so the installed fleet does NOT re-notify every already-seen directive
# on the first delegated tick. `_load_listen_state` fills the remaining defaults.
OLD_KEY="$(printf '%s-%s' "$TEAM" "$AGENT" | tr -c 'A-Za-z0-9_.-' '-')-$(printf '%s-%s' "$TEAM" "$AGENT" | cksum | cut -d' ' -f1)"
ITEMS_FILE="$STATE_DIR/listener-$OLD_KEY.items"
if [[ -n "$STATE_FILE" && ! -e "$STATE_FILE" && -f "$ITEMS_FILE" ]]; then
  # ids are directive slugs (slug-safe); strip anything else defensively, one per
  # line -> a JSON array. No re-notify: these ids start life already "seen".
  SEED="$(sed 's/[^A-Za-z0-9_.:-]//g' "$ITEMS_FILE" | awk 'NF' | sed 's/.*/"&"/' | paste -sd, - || true)"
  printf '{"inbox_ids":[%s],"response_keys":[],"slug_owned":{}}\n' "$SEED" > "$STATE_FILE" || true
fi

# Delegate one tick. Each new event prints a line to stdout; a degraded transport
# prints LISTEN DEGRADED to stderr. The verb advances its own state; never fail
# the schedule (|| true), matching the old contract.
OUT="$(coord-engine listen "$TEAM" --agent "$AGENT" --once 2>/dev/null || true)"
NEW="$(printf '%s\n' "$OUT" | sed '/^$/d' | wc -l | tr -d '[:space:]')"

if [[ "$NEW" -gt 0 ]]; then
  MSG="coord: ${NEW} new event(s) for ${AGENT} in team/${TEAM}"
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
  echo "$(date -u +%FT%TZ) no new events for ${AGENT}/${TEAM}"
fi
exit 0

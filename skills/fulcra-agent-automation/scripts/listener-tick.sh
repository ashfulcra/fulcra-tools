#!/usr/bin/env bash
# listener-tick.sh — one scheduled coord check for an agent, delegating to the
# engine's `listen` verb (the SINGLE implementation of the diff/notify logic; the
# tick used to hand-roll an `inbox --json` id-diff). Invoked by the job
# install-listener.sh creates (or manually). On NEW events since the last tick —
# new inbox directives OR responses to directives this agent owns — a macOS
# notification (osascript) or a log line, and optionally a consent-gated wake
# command. Quiet ticks emit nothing by default. Transport/engine degradation is
# forwarded to stderr and wakes the consented adapter once (``listen`` itself
# de-duplicates degradation per source/streak). Exit 0 always (a tick never
# fails the schedule).
#
# Adaptive mode keeps the scheduler itself simple: it may invoke this script at
# the active cadence, while a tiny local due-time file suppresses unnecessary
# coord/Fulcra reads after the hot tail expires.  No model is involved in either
# path.  Without a source-side push signal, idle-minutes is the maximum added
# pickup latency for work that arrives while cold.
#
# Usage: listener-tick.sh [--adaptive --active-minutes N --tail-minutes N
#                         --idle-minutes N] <team> <agent> [wake-cmd...]
set -euo pipefail

ADAPTIVE=0
ACTIVE_MINUTES=1
TAIL_MINUTES=30
IDLE_MINUTES=30
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --adaptive) ADAPTIVE=1;;
    --active-minutes) ACTIVE_MINUTES="${2:?--active-minutes needs an integer}"; shift;;
    --tail-minutes) TAIL_MINUTES="${2:?--tail-minutes needs an integer}"; shift;;
    --idle-minutes) IDLE_MINUTES="${2:?--idle-minutes needs an integer}"; shift;;
    --) shift; break;;
    --*) echo "unknown option: $1" >&2; exit 2;;
    *) break;;
  esac
  shift
done

TEAM="${1:?usage: listener-tick.sh [adaptive options] <team> <agent> [wake-cmd...]}"
AGENT="${2:?usage: listener-tick.sh [adaptive options] <team> <agent> [wake-cmd...]}"
shift 2 || true

if [[ "$ADAPTIVE" == "1" ]]; then
  [[ "$ACTIVE_MINUTES" =~ ^[0-9]+$ ]] && (( ACTIVE_MINUTES >= 1 )) || {
    echo "error: --active-minutes must be a positive integer" >&2; exit 2; }
  [[ "$TAIL_MINUTES" =~ ^[0-9]+$ ]] || {
    echo "error: --tail-minutes must be a non-negative integer" >&2; exit 2; }
  [[ "$IDLE_MINUTES" =~ ^[0-9]+$ ]] && (( IDLE_MINUTES >= ACTIVE_MINUTES )) || {
    echo "error: --idle-minutes must be an integer >= --active-minutes" >&2; exit 2; }
fi

STATE_DIR="${COORD_LISTENER_STATE:-$HOME/.cache/coord-engine}"
mkdir -p "$STATE_DIR"

OLD_KEY="$(printf '%s-%s' "$TEAM" "$AGENT" | tr -c 'A-Za-z0-9_.-' '-')-$(printf '%s-%s' "$TEAM" "$AGENT" | cksum | cut -d' ' -f1)"
CADENCE_FILE="$STATE_DIR/listener-$OLD_KEY.cadence"
WAKE_PENDING_FILE="$STATE_DIR/listener-$OLD_KEY.wake-pending"
NOW="${COORD_LISTENER_NOW_EPOCH:-$(date +%s)}"
[[ "$NOW" =~ ^[0-9]+$ ]] || { echo "error: COORD_LISTENER_NOW_EPOCH must be an epoch integer" >&2; exit 2; }

ACTIVE_UNTIL=0
NEXT_DUE=0
FAILURE_STREAK=0
if [[ "$ADAPTIVE" == "1" && -f "$CADENCE_FILE" ]]; then
  ACTIVE_UNTIL="$(awk -F= '$1 == "active_until" {print $2; exit}' "$CADENCE_FILE" 2>/dev/null || true)"
  NEXT_DUE="$(awk -F= '$1 == "next_due" {print $2; exit}' "$CADENCE_FILE" 2>/dev/null || true)"
  FAILURE_STREAK="$(awk -F= '$1 == "failure_streak" {print $2; exit}' "$CADENCE_FILE" 2>/dev/null || true)"
  [[ "$ACTIVE_UNTIL" =~ ^[0-9]+$ ]] || ACTIVE_UNTIL=0
  [[ "$NEXT_DUE" =~ ^[0-9]+$ ]] || NEXT_DUE=0
  [[ "$FAILURE_STREAK" =~ ^[0-9]+$ ]] || FAILURE_STREAK=0
fi

# Load durable wake-retry state before the cadence gate.  A cold listener must
# wake at min(next listener due, pending wake retry due); otherwise an already
# due exact-session wake can be delayed by the full idle cadence.
PENDING_WAKE=0
WAKE_FAILURE_STREAK=0
WAKE_RETRY_DUE=0
PENDING_WAKE_DUE=0
PENDING_EVENT_REFS=""
if [[ -f "$WAKE_PENDING_FILE" ]]; then
  PENDING_WAKE=1
  WAKE_FAILURE_STREAK="$(awk -F= '$1 == "failure_streak" {print $2; exit}' "$WAKE_PENDING_FILE" 2>/dev/null || true)"
  WAKE_RETRY_DUE="$(awk -F= '$1 == "retry_due" {print $2; exit}' "$WAKE_PENDING_FILE" 2>/dev/null || true)"
  PENDING_EVENT_REFS="$(awk -F= '$1 == "event_refs" {sub(/^[^=]*=/, ""); print; exit}' "$WAKE_PENDING_FILE" 2>/dev/null || true)"
  [[ "$WAKE_FAILURE_STREAK" =~ ^[0-9]+$ ]] || WAKE_FAILURE_STREAK=0
  [[ "$WAKE_RETRY_DUE" =~ ^[0-9]+$ ]] || WAKE_RETRY_DUE=0
  [[ -z "$PENDING_EVENT_REFS" || "$PENDING_EVENT_REFS" =~ ^[A-Z]+:[A-Za-z0-9:_.-]+(,[A-Z]+:[A-Za-z0-9:_.-]+)*$ ]] || PENDING_EVENT_REFS=""
  (( NOW >= WAKE_RETRY_DUE )) && PENDING_WAKE_DUE=1
fi

EFFECTIVE_DUE="$NEXT_DUE"
if [[ "$PENDING_WAKE" == "1" && ( "$EFFECTIVE_DUE" -eq 0 || "$WAKE_RETRY_DUE" -lt "$EFFECTIVE_DUE" ) ]]; then
  EFFECTIVE_DUE="$WAKE_RETRY_DUE"
fi
if [[ "$ADAPTIVE" == "1" && "${COORD_LISTENER_FORCE:-0}" != "1" && "$NOW" -lt "$EFFECTIVE_DUE" ]]; then
  if [[ "${COORD_LISTENER_VERBOSE:-0}" == "1" ]]; then
    echo "$(date -u +%FT%TZ) adaptive listener not due until epoch ${EFFECTIVE_DUE} for ${AGENT}/${TEAM}"
  fi
  exit 0
fi

# The engine owns the state/diff logic now; the tick is a thin delegate. Resolve
# coord-engine the same way as before: bare, from the job's pinned PATH. Ask the
# engine for its state-file path (slugify/agent_key naming lives there, not here).
STATE_FILE="$(coord-engine listen "$TEAM" --agent "$AGENT" --state-path 2>/dev/null || true)"

# One-time migration: earlier ticks tracked seen inbox ids in a `.items` file. If
# the engine's listen-state does not exist yet but that file does, seed inbox_ids
# from it so the installed fleet does NOT re-notify every already-seen directive
# on the first delegated tick. `_load_listen_state` fills the remaining defaults.
ITEMS_FILE="$STATE_DIR/listener-$OLD_KEY.items"
if [[ -n "$STATE_FILE" && ! -e "$STATE_FILE" && -f "$ITEMS_FILE" ]]; then
  # ids are directive slugs (slug-safe); strip anything else defensively, one per
  # line -> a JSON array. No re-notify: these ids start life already "seen".
  SEED="$(sed 's/[^A-Za-z0-9_.:-]//g' "$ITEMS_FILE" | awk 'NF' | sed 's/.*/"&"/' | paste -sd, - || true)"
  printf '{"inbox_ids":[%s],"response_keys":[],"slug_owned":{}}\n' "$SEED" > "$STATE_FILE" || true
fi

# Delegate one tick. Keep stderr: LISTEN DEGRADED is fail-visible evidence, not
# scheduler noise. A temporary file avoids merging diagnostics into the event
# count while preserving multi-line errors exactly.
ERR_FILE="$(mktemp "$STATE_DIR/listener-error.XXXXXX")"
trap 'rm -f "$ERR_FILE"' EXIT
RC=0
OUT="$(coord-engine listen "$TEAM" --agent "$AGENT" --once 2>"$ERR_FILE")" || RC=$?
ERR="$(cat "$ERR_FILE")"
DEGRADATION_PULSE=0
if [[ -n "$ERR" ]]; then
  printf '%s\n' "$ERR" >&2
  DEGRADATION_PULSE=1
elif [[ "$RC" -ne 0 && "$RC" -ne 3 ]]; then
  # Exit 3 is the engine's persistent-degradation state. It deliberately emits
  # stderr only for a new source pulse, so an empty-stderr 3 must remain quiet
  # while still driving retry backoff. Other silent failures are unexpected and
  # need a synthetic fail-visible pulse.
  printf 'LISTEN DEGRADED: engine exited %s (no stderr)\n' "$RC" >&2
  DEGRADATION_PULSE=1
fi
NEW="$(printf '%s\n' "$OUT" | sed '/^$/d' | wc -l | tr -d '[:space:]')"
DEGRADED=0
[[ -z "$ERR" && "$RC" -eq 0 ]] || DEGRADED=1
# Extract only the event kind + canonical slug from the engine's fixed text
# envelope. Titles, outcomes, authors, and every other bus-controlled string are
# deliberately excluded. Wake adapters can use these refs to orient the resumed
# session without paying for a blind broad board read or injecting raw bus text.
EVENT_REFS="$(printf '%s\n' "$OUT" | awk '
  /^(DIRECTIVE|RESPONSE|VERDICT|SETTLED|ORPHAN) / {
    kind=$1; slug=$2
    if (count < 20 && length(slug) <= 200 && slug ~ /^[A-Za-z0-9_.:-]+$/) {
      if (count++) printf ","
      printf "%s:%s", kind, slug
    }
  }
')"

if [[ "$ADAPTIVE" == "1" ]]; then
  # Only affirmative work activity heats the work tail. Transport degradation
  # is a separate state: treating it as work lets a chronic outage pin every
  # listener at the active cadence forever. A successful fresh install still
  # starts hot for one tail window so pre-existing work can settle.
  if [[ "$NEW" -gt 0 || ( "$DEGRADED" -eq 0 && "$ACTIVE_UNTIL" -eq 0 ) ||
        "${COORD_LISTENER_MARK_ACTIVE:-0}" == "1" ]]; then
    ACTIVE_UNTIL=$(( NOW + TAIL_MINUTES * 60 ))
  fi

  if [[ "$DEGRADED" -eq 1 ]]; then
    # Exponential local retry backoff, capped at the ordinary idle cadence.
    # The engine already emits each degraded source once per streak; this keeps
    # the token-free host probe responsive to transient failures without making
    # a persistent outage generate one Fulcra read every active interval.
    FAILURE_STREAK=$(( FAILURE_STREAK + 1 ))
    RETRY_MINUTES="$ACTIVE_MINUTES"
    RETRY_STEP=1
    while (( RETRY_STEP < FAILURE_STREAK && RETRY_MINUTES < IDLE_MINUTES )); do
      RETRY_MINUTES=$(( RETRY_MINUTES * 2 ))
      (( RETRY_MINUTES > IDLE_MINUTES )) && RETRY_MINUTES="$IDLE_MINUTES"
      RETRY_STEP=$(( RETRY_STEP + 1 ))
    done
    NEXT_DUE=$(( NOW + RETRY_MINUTES * 60 ))
  else
    FAILURE_STREAK=0
    if [[ "$NOW" -lt "$ACTIVE_UNTIL" ]]; then
      NEXT_DUE=$(( NOW + ACTIVE_MINUTES * 60 ))
    else
      NEXT_DUE=$(( NOW + IDLE_MINUTES * 60 ))
    fi
  fi
  CADENCE_TMP="$(mktemp "$STATE_DIR/listener-cadence.XXXXXX")"
  printf 'active_until=%s\nnext_due=%s\nfailure_streak=%s\n' \
    "$ACTIVE_UNTIL" "$NEXT_DUE" "$FAILURE_STREAK" > "$CADENCE_TMP"
  mv "$CADENCE_TMP" "$CADENCE_FILE"
fi

if [[ "$NEW" -eq 0 && "$PENDING_WAKE_DUE" -eq 1 && -n "$PENDING_EVENT_REFS" ]]; then
  EVENT_REFS="$PENDING_EVENT_REFS"
fi
if [[ "$NEW" -gt 0 || "$DEGRADATION_PULSE" -eq 1 ||
      ( "$PENDING_WAKE_DUE" -eq 1 && "$#" -gt 0 ) ]]; then
  if [[ "$NEW" -gt 0 ]]; then
    MSG="coord: ${NEW} new event(s) for ${AGENT} in team/${TEAM}"
  elif [[ "$PENDING_WAKE" -eq 1 && "$DEGRADED" -eq 0 ]]; then
    MSG="coord: retrying pending wake for ${AGENT} in team/${TEAM}"
  else
    MSG="coord: listener degraded for ${AGENT} in team/${TEAM}"
  fi
  echo "$(date -u +%FT%TZ) $MSG"
  if [[ "$NEW" -gt 0 || "$DEGRADATION_PULSE" -eq 1 ]] && command -v osascript >/dev/null 2>&1; then
    # display only; TEAM/AGENT are validated by the installer, but escape quotes anyway
    SAFE_MSG="${MSG//\"/}"
    osascript -e "display notification \"${SAFE_MSG}\" with title \"coord inbox\"" || true
  fi
  if [[ "$#" -gt 0 ]]; then
    # consent-gated wake command (installer requires explicit --wake-cmd)
    # Fixed metadata lets harness adapters fetch the authoritative briefing;
    # event text is advisory only and is never interpreted as shell source.
    WAKE_RC=0
    env COORD_LISTENER_TEAM="$TEAM" COORD_LISTENER_AGENT="$AGENT" \
      COORD_LISTENER_DEGRADED="$DEGRADED" COORD_LISTENER_OUTPUT="$OUT" \
      COORD_LISTENER_EVENT_REFS="$EVENT_REFS" \
      COORD_LISTENER_RETRY="$PENDING_WAKE" \
      "$@" || WAKE_RC=$?
    if [[ "$WAKE_RC" -eq 0 ]]; then
      rm -f "$WAKE_PENDING_FILE"
    else
      WAKE_FAILURE_STREAK=$(( WAKE_FAILURE_STREAK + 1 ))
      WAKE_RETRY_MINUTES="$ACTIVE_MINUTES"
      WAKE_RETRY_STEP=1
      while (( WAKE_RETRY_STEP < WAKE_FAILURE_STREAK && WAKE_RETRY_MINUTES < IDLE_MINUTES )); do
        WAKE_RETRY_MINUTES=$(( WAKE_RETRY_MINUTES * 2 ))
        (( WAKE_RETRY_MINUTES > IDLE_MINUTES )) && WAKE_RETRY_MINUTES="$IDLE_MINUTES"
        WAKE_RETRY_STEP=$(( WAKE_RETRY_STEP + 1 ))
      done
      WAKE_RETRY_DUE=$(( NOW + WAKE_RETRY_MINUTES * 60 ))
      PENDING_TMP="$(mktemp "$STATE_DIR/listener-wake-pending.XXXXXX")"
      printf 'failed_at=%s\nexit=%s\nfailure_streak=%s\nretry_due=%s\nevent_refs=%s\n' \
        "$NOW" "$WAKE_RC" "$WAKE_FAILURE_STREAK" "$WAKE_RETRY_DUE" \
        "$EVENT_REFS" > "$PENDING_TMP"
      mv "$PENDING_TMP" "$WAKE_PENDING_FILE"
      echo "$(date -u +%FT%TZ) wake command failed (exit ${WAKE_RC}); retry armed for epoch ${WAKE_RETRY_DUE}" >&2
    fi
  fi
elif [[ "${COORD_LISTENER_VERBOSE:-0}" == "1" ]]; then
  echo "$(date -u +%FT%TZ) no new events for ${AGENT}/${TEAM}"
fi
exit 0

#!/bin/bash
# fulcra-primitives-maintainer — the shared alert path. Sourced, never run.
#
# Both jobs (daily drift-check.sh, weekly weekly-review.sh) reach a human the
# same way: post a directive on the coord team bus. Until 2026-07-16 that path
# was a few copy-pasted lines in each script, and BOTH copies were addressed to
#
#     claude-code:Mac:fulcra-primitives-maintainer
#
# — one host's session identity, baked into a script that ships to every host,
# and live on none of them. When the daily fired correctly at 2026-07-16T16:11:50
# with real drift, its P1 went to that identity: present in the presence roster,
# last beat 8 days earlier, read by nobody. `tell` returned 0. The drift was
# found by hand.
#
# The rule this file exists to enforce: a message nobody can receive is not an
# alert and must never look like one. So:
#
#   - The SENDER is resolved at runtime, from the same env var the engine itself
#     reads (FULCRA_COORD_AGENT), never minted here. A script that invents an
#     identity produces a phantom that diverges from the engine's resolver — the
#     same family of bug as the id we are removing.
#   - The TARGET is a ROLE, not a named agent. Sessions stop; the role outlives
#     them, and whoever holds it is by definition the one who should act.
#   - Reachability is verified on EVERY run, including clean ones. Routing rot is
#     silent by construction: it costs nothing until the day it matters, which is
#     the day you find out. The check is the alert path's positive control — an
#     alert path that cannot deliver is not "no news".
#   - Every failure of the path is loud LOCALLY — stderr, a marker file, and a
#     non-zero exit — because the one thing a dead mailbox cannot do is tell you
#     it is dead.
#
# Callers must set STATE (the state dir) before sourcing, then call prim_init.

# --- configuration ---------------------------------------------------------
PRIM_TEAM="${PRIMITIVES_TEAM:-fulcra}"
# WHO the alert is for. A role: whoever holds fulcra-primitives-maintainer.
PRIM_TARGET="${PRIMITIVES_TARGET:-fulcra-primitives-maintainer}"
# role|agent — changes only HOW reachability is verified, never whether it is.
# `role`  -> `roles status`: HELD/CONTESTED with a fresh holder is reachable.
# `agent` -> `presence show`: live|idle is reachable (the engine's own broadcast
#            reach: everyone not stale). Provided because a host may deliberately
#            aim at one named agent; it gets the same verification, not a pass.
PRIM_TARGET_KIND="${PRIMITIVES_TARGET_KIND:-role}"
PRIM_WORKSTREAM="${PRIMITIVES_WORKSTREAM:-fulcra-primitives}"
PRIM_CE="${PRIMITIVES_COORD_ENGINE:-$(command -v coord-engine || echo "$HOME/.local/bin/coord-engine")}"

# The alert-path-is-broken marker. A THIRD debt, deliberately not folded into the
# other two: DRIFT-ALERT.txt means a doc rewrite is owed, PROBE-UNKNOWN.txt means
# a probe needs fixing, and this means nobody would have heard either one. It has
# its own owner (whoever runs the fleet's roles/presence) and its own discharge
# condition (the target answering), so it gets its own file.
PRIM_UNDELIVERED=""

PRIM_SENDER=""
PRIM_SENDER_ARGS=()
PRIM_PROBLEMS=()
PRIM_TARGET_STATE="not checked"

# Callers define ts()/log() before sourcing; these are the fallbacks so the lib
# never depends on the caller's dialect.
if ! declare -f ts >/dev/null 2>&1; then
  ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
fi
if ! declare -f log >/dev/null 2>&1; then
  log() { :; }
fi

# --- identity --------------------------------------------------------------
# Resolve the sender the way coord-engine resolves it: FULCRA_COORD_AGENT, else
# the engine's own host fallback. PRIMITIVES_AGENT is accepted first so a host
# can give these jobs an identity distinct from the session's.
#
# An unresolved sender is loud but NOT fatal, and the fallback is the engine's,
# not ours: when nothing is set we omit --from entirely and let coord-engine
# derive `coord-reconcile:<hostname>` itself. Two reasons. (1) Minting an id here
# is how `claude-code:Mac:...` happened — a second resolver that agrees with the
# engine right up until it doesn't. (2) Refusing to send because the job cannot
# name ITSELF would suppress an alert over a cosmetic defect; the sender only
# addresses the reply leg, and nobody is listening as a cron job. The address
# that has to be right is the target's.
prim_resolve_sender() {
  PRIM_SENDER="${PRIMITIVES_AGENT:-${FULCRA_COORD_AGENT:-}}"
  # An un-substituted install placeholder is not an identity, and it is worse
  # than none: it type-checks, the bus accepts it, and it addresses nobody. Treat
  # it as unset and say so.
  case "$PRIM_SENDER" in
    __*__)
      echo "primitives-alert: sender is the literal install placeholder '$PRIM_SENDER' —" >&2
      echo "  the plist was copied without substituting it. Ignoring it." >&2
      log "sender: placeholder $PRIM_SENDER ignored"
      PRIM_SENDER="" ;;
  esac
  if [ -n "$PRIM_SENDER" ]; then
    PRIM_SENDER_ARGS=(--from "$PRIM_SENDER")
    log "sender: $PRIM_SENDER"
    return 0
  fi
  PRIM_SENDER_ARGS=()
  echo "primitives-alert: no PRIMITIVES_AGENT / FULCRA_COORD_AGENT set — the tell will be" >&2
  echo "  owned by coord-engine's own host fallback (coord-reconcile:\$(hostname)). Alerts" >&2
  echo "  still deliver; replies have no named sender to return to. Set FULCRA_COORD_AGENT" >&2
  echo "  in the launchd plist to fix." >&2
  log "sender: UNSET (deferring to coord-engine's host fallback)"
}

# --- problems --------------------------------------------------------------
prim_problem() {
  PRIM_PROBLEMS[${#PRIM_PROBLEMS[@]}]="$1"
  echo "primitives-alert: $1" >&2
  log "ALERT PATH: $1"
}

prim_alert_path_ok() { [ ${#PRIM_PROBLEMS[@]} -eq 0 ]; }

# --- reachability ----------------------------------------------------------
# Verify the target can receive. Fail-closed at every step: an unparseable or
# failed lookup is UNKNOWN — a problem — never "assume it's fine". "I could not
# check whether anyone is listening" and "someone is listening" must not produce
# the same run.
prim_check_target() {
  local out rc verdict
  if [ "$PRIM_TARGET_KIND" = "agent" ]; then
    out="$("$PRIM_CE" presence show "$PRIM_TEAM" --json 2>&1)"; rc=$?
    if [ $rc -ne 0 ]; then
      PRIM_TARGET_STATE="UNKNOWN"
      prim_problem "cannot verify target: \`presence show $PRIM_TEAM\` exited $rc ($(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200))"
      return 1
    fi
    verdict="$(TARGET="$PRIM_TARGET" TEAM="$PRIM_TEAM" python3 -c '
import json, os, sys
try:
    ros = json.load(sys.stdin)
    if not isinstance(ros, list):
        raise ValueError("roster is not a list")
except Exception as e:
    print("UNKNOWN|roster did not parse (%s)" % (e,)); raise SystemExit(0)
t = os.environ["TARGET"]
hit = [r for r in ros if isinstance(r, dict) and r.get("agent") == t]
if not hit:
    print("ABSENT|%s has no presence shard at all in team/%s — it has never beaten, "
          "or the id is a typo" % (t, os.environ.get("TEAM", "?")))
    raise SystemExit(0)
liv = hit[0].get("liveness")
seen = hit[0].get("last_seen")
# The engine treats not-stale as reachable (its broadcast roster). Match it
# rather than inventing a second liveness rule.
print("%s|%s liveness=%s last_seen=%s" % (
    "OK" if liv in ("live", "idle") else "DEAD", t, liv, seen))
' <<<"$out" 2>/dev/null)"
  else
    out="$("$PRIM_CE" roles status "$PRIM_TEAM" "$PRIM_TARGET" --json 2>&1)"; rc=$?
    if [ $rc -ne 0 ]; then
      # rc 1 here is the engine's own fail-closed signal: the lease listing was
      # unreadable, so the role's state is UNKNOWN — explicitly NOT vacant.
      PRIM_TARGET_STATE="UNKNOWN"
      prim_problem "cannot verify target: \`roles status $PRIM_TEAM $PRIM_TARGET\` exited $rc ($(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200))"
      return 1
    fi
    verdict="$(python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    status = d["status"]
except Exception as e:
    print("UNKNOWN|roles status did not parse (%s)" % (e,)); raise SystemExit(0)
fresh = d.get("fresh_holders")
if not isinstance(fresh, list):
    print("UNKNOWN|roles status has no fresh_holders list"); raise SystemExit(0)
if status in ("HELD", "CONTESTED") and fresh:
    print("OK|%s held by %s" % (d.get("role"), ", ".join(map(str, fresh))))
else:
    print("DEAD|role %s is %s (no fresh holder). A directive to a role nobody holds "
          "is delivered to nothing." % (d.get("role"), status))
' <<<"$out" 2>/dev/null)"
  fi

  # An empty verdict means the parser itself died: UNKNOWN, not reachable.
  case "${verdict:-}" in
    OK\|*)
      PRIM_TARGET_STATE="${verdict#OK|}"
      log "target reachable: $PRIM_TARGET_STATE"
      return 0 ;;
    DEAD\|*|ABSENT\|*)
      PRIM_TARGET_STATE="UNREACHABLE — ${verdict#*|}"
      prim_problem "alert target is unreachable: ${verdict#*|}"
      return 1 ;;
    *)
      PRIM_TARGET_STATE="UNKNOWN"
      prim_problem "cannot verify target ${PRIM_TARGET}: ${verdict:-reachability check produced no output (python3 missing, or it crashed)}"
      return 1 ;;
  esac
}

# --- delivery --------------------------------------------------------------
# `tell` drops a message (rc 1, "already exists") when a slug prefix collides, so
# the rc is checked, logged, AND turned into a delivery problem. It used to be
# logged only — to a file under the state dir that nothing reads. A tell that
# failed and a tell that landed produced the same run.
prim_notify() {
  local prio="$1" title="$2" summary="$3" out rc
  out="$("$PRIM_CE" tell "$PRIM_TEAM" "$PRIM_TARGET" "$title" \
        ${PRIM_SENDER_ARGS[@]+"${PRIM_SENDER_ARGS[@]}"} \
        --workstream "$PRIM_WORKSTREAM" --priority "$prio" --summary "$summary" 2>&1)"
  rc=$?
  if [ $rc -ne 0 ]; then
    prim_problem "tell FAILED rc=$rc — this alert was NOT delivered: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-300)"
  else
    log "tell ok ($PRIM_TARGET): $title"
  fi
  return $rc
}

# --- the marker ------------------------------------------------------------
# Write (or clear) ALERT-UNDELIVERED.txt. Call once, last, on every code path:
# the marker's whole job is to survive a run that otherwise looked fine.
# $1: one line of context — what this run was trying to say.
prim_flush() {
  local context="${1:-}"
  if prim_alert_path_ok; then
    if [ -f "$PRIM_UNDELIVERED" ]; then
      log "alert path recovered ($PRIM_TARGET_STATE); clearing $PRIM_UNDELIVERED"
      rm -f "$PRIM_UNDELIVERED"
    fi
    return 0
  fi
  {
    echo "ALERT PATH BROKEN $(ts)"
    echo
    echo "This run's alerts could not be shown to be delivered to anyone. Whatever"
    echo "else this job reported, treat it as UNHEARD: on 2026-07-16 a correct P1"
    echo "went to an identity that had not beaten in 8 days, \`tell\` returned 0,"
    echo "and the drift was found by hand a day later."
    echo
    if [ -n "$context" ]; then
      echo "WHAT THIS RUN WAS TRYING TO SAY: $context"
    else
      echo "This run had nothing to report. That is exactly when the path rots"
      echo "unnoticed — so it is checked on clean runs too, and this file is the"
      echo "check failing. The next real alert would have gone nowhere."
    fi
    echo
    echo "TARGET: $PRIM_TARGET (kind=$PRIM_TARGET_KIND, team=$PRIM_TEAM)"
    echo "STATE:  $PRIM_TARGET_STATE"
    echo "SENDER: ${PRIM_SENDER:-<unset — coord-engine host fallback>}"
    echo
    echo "PROBLEMS:"
    printf '  - %s\n' "${PRIM_PROBLEMS[@]}"
    echo
    echo "FIX: give the role a live holder"
    echo "  coord-engine roles claim $PRIM_TEAM $PRIM_TARGET --agent <your-id>"
    echo "or repoint the job (PRIMITIVES_TARGET / PRIMITIVES_TARGET_KIND) at an"
    echo "identity that is actually live. The next run whose target answers clears"
    echo "this file itself."
  } > "$PRIM_UNDELIVERED"
  return 1
}

# --- exit ------------------------------------------------------------------
# Exit, having last written the delivery verdict. EVERY exit in both scripts goes
# through this: a marker whose job is to survive a run that looked fine cannot be
# on a path that only some exits take.
# $1: the exit code this run earned on its own terms. $2: what it was trying to say.
finish() {
  local rc="$1" context="${2:-}"
  if ! prim_flush "$context"; then
    # An undeliverable alert outranks a clean bill of health, and only that: a
    # drift is still a drift (1) and an unobserved surface is still UNKNOWN (2).
    # Those already mean "a human is needed" — overwriting them with 3 would
    # trade a specific summons for a vaguer one. 3 is for the run that would
    # otherwise have said "all clear" while shouting into a void.
    [ "$rc" = "0" ] && rc=3
  fi
  exit "$rc"
}

# --- init ------------------------------------------------------------------
prim_init() {
  PRIM_UNDELIVERED="$STATE/ALERT-UNDELIVERED.txt"
  prim_resolve_sender
  prim_check_target || true   # a broken path never aborts the run; it marks it
}

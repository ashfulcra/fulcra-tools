#!/bin/bash
# rollback-probe.sh — detect container filesystem rollback with EXTERNAL evidence.
#
# Design: Michael's append+external-log experiment, upgraded per Greg with
# git-bundle verification (Customer Data Engineering, 2026-08-01):
#   1. every tick APPENDS seq=N to a local file AND commits it to a local git
#      repo — the container-side history;
#   2. every tick writes an immutable Fulcra record row {"probe":"rollback",
#      seq,ts,boot} — the external monotonic log Michael asked for — and
#      uploads the git bundle + latest.json to the File Store — Greg's
#      authoritative external repo history;
#   3. every tick FIRST verifies local state against the external history.
#      If the local file/repo is ever BEHIND what the external log proves was
#      written, that is a detected rollback: an incident doc is uploaded with
#      both histories' positions and timestamps.
# Distinguishes three states on divergence:
#   wiped     — local probe state absent entirely (fresh scratchpad)
#   rollback  — local present but seq/commits BEHIND the external history
#   write-gap — local AHEAD of external (an earlier upload failed; not rollback)
# Self-contained; safe to re-run anytime (the wake ceremony re-arms it).
set -u
SP="${ROLLBACK_PROBE_HOME:-/tmp/claude-0/-home-user-fulcra-tools/a07b97e8-9d5f-59f3-8df6-9ceba3d40af6/scratchpad}"
PD="$SP/rollback-probe"; LOG="$PD/appends.log"; GITD="$PD/repo"
STORE="team/fulcra/_coord/agents/coord-boss/rollback-probe"
TYPE="MomentAnnotation/ea49d0d3-acb7-49c6-93b6-bee81d126c92"
BOOT=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)
INTERVAL="${ROLLBACK_PROBE_INTERVAL:-120}"
GIT="git -C $GITD -c user.name=rollback-probe -c user.email=probe@local"
mkdir -p "$PD"

now() { date -u +%FT%TZ; }

verify() {  # returns via echo: ok | first-run | wiped | rollback | write-gap
  rm -f "$PD/latest.json"
  fulcra-api file download "$STORE/latest.json" "$PD/latest.json" >/dev/null 2>&1
  if [ ! -s "$PD/latest.json" ]; then echo "first-run"; return; fi
  EXT_SEQ=$(python3 -c "import json;print(json.load(open('$PD/latest.json'))['seq'])" 2>/dev/null || echo 0)
  EXT_HEAD=$(python3 -c "import json;print(json.load(open('$PD/latest.json'))['head'])" 2>/dev/null || echo none)
  EXT_TS=$(python3 -c "import json;print(json.load(open('$PD/latest.json'))['ts'])" 2>/dev/null || echo none)
  if [ ! -f "$LOG" ] || [ ! -d "$GITD/.git" ]; then
    incident wiped "$EXT_SEQ" 0 "$EXT_HEAD" none "$EXT_TS"; echo "wiped"; return
  fi
  LOCAL_SEQ=$(tail -1 "$LOG" 2>/dev/null | grep -o 'seq=[0-9]*' | cut -d= -f2); LOCAL_SEQ=${LOCAL_SEQ:-0}
  LOCAL_HEAD=$($GIT rev-parse HEAD 2>/dev/null || echo none)
  HAVE_EXT_COMMIT=yes
  [ "$EXT_HEAD" != none ] && ! $GIT cat-file -e "$EXT_HEAD" 2>/dev/null && HAVE_EXT_COMMIT=no
  if [ "$LOCAL_SEQ" -lt "$EXT_SEQ" ] || [ "$HAVE_EXT_COMMIT" = no ]; then
    incident rollback "$EXT_SEQ" "$LOCAL_SEQ" "$EXT_HEAD" "$LOCAL_HEAD" "$EXT_TS"; echo "rollback"; return
  fi
  if [ "$LOCAL_SEQ" -gt "$EXT_SEQ" ]; then echo "write-gap"; return; fi
  echo "ok"
}

incident() {  # kind ext_seq local_seq ext_head local_head ext_ts
  TS=$(now)
  cat > "$PD/incident.json" <<EOJ
{"kind":"$1","detected_at":"$TS","boot_id":"$BOOT",
 "external":{"seq":$2,"head":"$4","last_write_ts":"$6"},
 "local":{"seq":$3,"head":"$5"},
 "meaning":"external history proves writes the local filesystem no longer has"}
EOJ
  fulcra-api file upload "$PD/incident.json" "$STORE/incidents/$TS-$1.json" >/dev/null 2>&1
  printf '{"note":"{\\"probe\\":\\"rollback-INCIDENT\\",\\"kind\\":\\"%s\\",\\"ext_seq\\":%s,\\"local_seq\\":%s,\\"ts\\":\\"%s\\",\\"boot\\":\\"%s\\"}"}' \
    "$1" "$2" "$3" "$TS" "$BOOT" | fulcra-api record "$TYPE" --api-version v1alpha1 --source=rollback-probe >/dev/null 2>&1
  echo "ROLLBACK-PROBE INCIDENT: $1 — external seq=$2 vs local seq=$3 (evidence: $STORE/incidents/$TS-$1.json)"
}

tick() {
  STATE=$(verify | tail -1)
  case "$STATE" in
    first-run) mkdir -p "$GITD"; [ -d "$GITD/.git" ] || git -C "$GITD" init -q; EXT_SEQ=0;;
    wiped|rollback) mkdir -p "$GITD"; [ -d "$GITD/.git" ] || git -C "$GITD" init -q;;
    *) : ;;
  esac
  EXT_SEQ=${EXT_SEQ:-$(python3 -c "import json;print(json.load(open('$PD/latest.json'))['seq'])" 2>/dev/null || echo 0)}
  LOCAL_SEQ=$(tail -1 "$LOG" 2>/dev/null | grep -o 'seq=[0-9]*' | cut -d= -f2); LOCAL_SEQ=${LOCAL_SEQ:-0}
  SEQ=$(( (LOCAL_SEQ > EXT_SEQ ? LOCAL_SEQ : EXT_SEQ) + 1 ))
  TS=$(now)
  echo "seq=$SEQ ts=$TS boot=$BOOT state=$STATE" >> "$LOG"
  cp "$LOG" "$GITD/appends.log"
  $GIT add appends.log >/dev/null 2>&1
  $GIT commit -q -m "probe seq=$SEQ $TS" >/dev/null 2>&1
  HEAD=$($GIT rev-parse HEAD 2>/dev/null || echo none)
  # external evidence, all three legs:
  printf '{"note":"{\\"probe\\":\\"rollback\\",\\"seq\\":%s,\\"ts\\":\\"%s\\",\\"boot\\":\\"%s\\",\\"head\\":\\"%s\\",\\"state\\":\\"%s\\"}"}' \
    "$SEQ" "$TS" "$BOOT" "$HEAD" "$STATE" | fulcra-api record "$TYPE" --api-version v1alpha1 --source=rollback-probe >/dev/null 2>&1
  $GIT bundle create "$PD/repo.bundle" --all >/dev/null 2>&1
  fulcra-api file upload "$PD/repo.bundle" "$STORE/repo.bundle" >/dev/null 2>&1
  printf '{"seq":%s,"head":"%s","ts":"%s","boot":"%s"}' "$SEQ" "$HEAD" "$TS" "$BOOT" > "$PD/latest.json"
  fulcra-api file upload "$PD/latest.json" "$STORE/latest.json" >/dev/null 2>&1
  echo "tick seq=$SEQ state=$STATE head=${HEAD:0:9}"
}

case "${1:-loop}" in
  tick) tick;;
  verify) verify;;
  loop)
    PIDF="$PD/probe.pid"
    if [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null && [ "$(cat $PIDF)" != "$$" ]; then exit 0; fi
    echo $$ > "$PIDF"
    while :; do tick; sleep "$INTERVAL"; done;;
esac

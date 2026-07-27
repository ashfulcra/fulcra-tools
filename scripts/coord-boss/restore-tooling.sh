#!/bin/bash
# Tycho self-heal: restore coord-boss tooling from the Fulcra File Store after a
# container rollback. Secrets are NOT stored here — linear.env / answers-linear.env
# come from environment config (BUS-74) or operator memory.
set -u
SS="/tmp/claude-0/-home-user-fulcra-tools/a07b97e8-9d5f-59f3-8df6-9ceba3d40af6/scratchpad"
BASE="team/fulcra/_coord/agents/coord-boss/stash"
for f in linear-sync.sh listener-loop.sh restore-tooling.sh; do
  [ -x "$SS/$f" ] || { fulcra-api file download "$BASE/$f" "$SS/$f" && chmod +x "$SS/$f" && echo "restored $f"; }
done

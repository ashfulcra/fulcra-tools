#!/bin/bash
# Install coord-boss duty tooling into a session scratchpad.
#
# WHY THIS EXISTS
# ---------------
# These scripts used to live only in a Fulcra File Store "stash", restored by
# the environment's setup script. That failed in a way worth remembering: the
# setup script restores a PINNED SNAPSHOT taken when the env config was
# written, not the live stash. On 2026-07-27 a fix added to one of these
# scripts was silently reverted by a container roll, and the self-heal
# reported success by staying quiet — it restored the files that were already
# in the snapshot and never noticed the new one was missing.
#
# The repo is cloned fresh at container start, so anything here always arrives
# current. That makes the repo the source of truth and the snapshot irrelevant.
#
# USAGE
#   bash scripts/coord-boss/bootstrap.sh [SCRATCHPAD_DIR]
#
# With no argument it derives the scratchpad from CLAUDE_SCRATCHPAD_DIR, else
# from the conventional per-session path. Idempotent: safe to run every boot.
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-${CLAUDE_SCRATCHPAD_DIR:-}}"

if [ -z "$DEST" ]; then
  # Conventional layout: /tmp/claude-*/<slug>/<session-uuid>/scratchpad.
  # Newest match wins; a session that has one will always have exactly one.
  DEST="$(ls -dt /tmp/claude-*/*/*/scratchpad 2>/dev/null | head -1)"
fi

if [ -z "$DEST" ]; then
  echo "bootstrap: no scratchpad found and none given" >&2
  echo "usage: bash scripts/coord-boss/bootstrap.sh /path/to/scratchpad" >&2
  exit 2
fi

mkdir -p "$DEST" || { echo "bootstrap: cannot create $DEST" >&2; exit 2; }

installed=0
for f in queue-sweep.sh linear-sync.sh restore-tooling.sh; do
  [ -f "$SRC/$f" ] || continue
  if cp "$SRC/$f" "$DEST/$f" && chmod +x "$DEST/$f"; then
    installed=$((installed + 1))
  else
    echo "bootstrap: FAILED to install $f" >&2
  fi
done

# Fail loudly on a partial install. A half-installed toolchain that reports
# success is the failure mode this script was written to end.
expected=$(ls "$SRC"/*.sh 2>/dev/null | grep -vc 'bootstrap\.sh$' || echo 0)
if [ "$installed" -ne "$expected" ]; then
  echo "bootstrap: installed $installed of $expected scripts into $DEST" >&2
  exit 1
fi

# Engine self-heal: container rolls restore a pre-v1.7 coord-engine that lacks
# the `queue` verb, blinding the session to the bus. Reinstall the pinned
# engine when the verb is missing. INSTALL ONLY — never run a queue read from
# here: this can execute before the agent wakes, and a cursor-advancing read
# whose output nobody processes silently discards wake hints.
if ! coord-engine queue --help >/dev/null 2>&1; then
  ENGINE_PIN="coord-engine-v1.7.2"
  ENGINE_SRC="git+https://github.com/ashfulcra/fulcra-tools@${ENGINE_PIN}#subdirectory=packages/coord-engine"
  if command -v uv >/dev/null 2>&1; then
    uv tool install --force fulcra-api >/dev/null 2>&1 || true
    uv tool install --force "$ENGINE_SRC" >/dev/null 2>&1 || true
  fi
  hash -r 2>/dev/null || true
  if coord-engine queue --help >/dev/null 2>&1; then
    echo "bootstrap: engine self-healed to $ENGINE_PIN"
  else
    echo "bootstrap: engine self-heal FAILED — queue verb still missing" >&2
    exit 1
  fi
fi

echo "bootstrap: installed $installed duty scripts into $DEST"

#!/bin/bash
# Tycho self-heal: unconditionally restore and manifest-verify coord-boss tooling
# from the Fulcra File Store after a container rollback. Secrets are NOT stored
# here — linear.env / answers-linear.env come from environment config (BUS-74)
# or operator memory.
set -euo pipefail
SS="${1:-/tmp/claude-0/-home-user-fulcra-tools/a07b97e8-9d5f-59f3-8df6-9ceba3d40af6/scratchpad}"
BASE="team/fulcra/_coord/agents/coord-boss/stash"
FILES=(linear-sync.sh restore-tooling.sh)
MANIFEST="$(mktemp)"
trap 'rm -f "$MANIFEST"' EXIT

# Read the manifest independently so a missing/corrupt manifest can never be
# mistaken for a current cache. Pull is unconditional: stale-but-present files
# are the rollback failure mode this entrypoint exists to repair.
fulcra-api file download "$BASE/manifest.json" "$MANIFEST" >/dev/null
coord-engine stash pull fulcra "${FILES[@]}" --agent coord-boss --dest "$SS"

python3 - "$MANIFEST" "$SS" "${FILES[@]}" <<'PY'
import hashlib
import json
import pathlib
import stat
import sys

manifest_path, destination, *names = sys.argv[1:]
try:
    manifest = json.loads(pathlib.Path(manifest_path).read_text(encoding="utf-8"))
    entries = manifest["files"]
    if not isinstance(entries, dict):
        raise TypeError("files is not an object")
except (OSError, ValueError, KeyError, TypeError) as exc:
    raise SystemExit(f"restore-tooling: manifest unreadable: {exc}")

for name in names:
    entry = entries.get(name)
    expected = entry.get("sha256") if isinstance(entry, dict) else None
    expected_exec = entry.get("exec") if isinstance(entry, dict) else None
    if not isinstance(expected, str) or not expected or not isinstance(expected_exec, bool):
        raise SystemExit(f"restore-tooling: incomplete manifest entry for {name}")
    path = pathlib.Path(destination, name)
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        actual_exec = bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    except OSError as exc:
        raise SystemExit(f"restore-tooling: cannot verify {name}: {exc}")
    if actual != expected:
        raise SystemExit(f"restore-tooling: checksum mismatch for {name}")
    if actual_exec != expected_exec:
        raise SystemExit(f"restore-tooling: executable-bit mismatch for {name}")

print(f"restore-tooling: restored and verified {len(names)} file(s)")
PY

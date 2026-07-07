#!/bin/bash
# Install coord2 lifecycle hooks for Claude Code / Cowork (idempotent).
# Usage: install-claude-code.sh <team> <agent>
#        install-claude-code.sh --uninstall <team> <agent>
#
# Coexistence: writes to ~/.claude/fulcra-coord2-hooks/ (distinct from the
# legacy ~/.claude/fulcra-coord-hooks/) and touches only its own command
# paths in settings.json, so legacy coord hooks keep working until the freeze.
set -euo pipefail
UNINSTALL=0
[ "${1:-}" = "--uninstall" ] && { UNINSTALL=1; shift; }
TEAM="${1:?team}"; AGENT="${2:?agent}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_DIR="$HOME/.claude/fulcra-coord2-hooks"
SETTINGS="$HOME/.claude/settings.json"

if [ "$UNINSTALL" -eq 0 ]; then
  mkdir -p "$HOOKS_DIR"
  for s in session-start pre-compact session-end; do
    sed -e "s/__TEAM__/$TEAM/g" -e "s/__AGENT__/$AGENT/g" \
      "$SRC_DIR/$s.sh" > "$HOOKS_DIR/$s.sh"
    chmod +x "$HOOKS_DIR/$s.sh"
  done
fi

python3 - "$SETTINGS" "$HOOKS_DIR" "$UNINSTALL" <<'EOF'
import json, os, sys
settings_path, hooks_dir, uninstall = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
# script -> (settings event, matcher). Per Task 0: live SessionStart entries
# carry this matcher string; PreCompact/SessionEnd entries have no matcher key.
mapping = {
    "SessionStart": ("session-start.sh", "startup|resume|clear|compact"),
    "PreCompact":   ("pre-compact.sh",   None),
    "SessionEnd":   ("session-end.sh",   None),
}
if os.path.exists(settings_path):
    with open(settings_path) as f:
        d = json.load(f)
else:
    d = {}
hooks = d.setdefault("hooks", {})
for event, (script, matcher) in mapping.items():
    cmd = f"{hooks_dir}/{script}"
    rules = hooks.setdefault(event, [])
    # dedupe: drop any prior entry for THIS exact command, then re-add.
    # Keys on our exact path only, so legacy/foreign entries are never disturbed.
    # A rule is dropped ONLY if removing our command is what emptied it; a rule
    # that never held our command (including one already empty or lacking a
    # hooks key) is left exactly as found.
    kept = []
    for r in rules:
        orig = r.get("hooks", [])
        if not any(h.get("command") == cmd for h in orig):
            kept.append(r)  # foreign — leave untouched
            continue
        r["hooks"] = [h for h in orig if h.get("command") != cmd]
        if r["hooks"]:
            kept.append(r)  # still has other hooks
        # else: our removal emptied it — drop
    rules[:] = kept
    if not uninstall:
        entry = {"hooks": [{"type": "command", "command": cmd}]}
        if matcher is not None:
            entry["matcher"] = matcher
        rules.append(entry)
    if not rules:
        del hooks[event]
with open(settings_path, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
print(("removed" if uninstall else "installed") + " coord2 hooks:",
      ", ".join(mapping))
EOF

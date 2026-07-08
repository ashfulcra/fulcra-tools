#!/usr/bin/env bash
# coord-setup.sh — install coord standalone from a checkout of this repo:
#   1. install the `coord-engine` tool from ./engine (so the engine + skills are
#      the SAME version by construction), and
#   2. install the 6 fulcra-agent-* skills into your agent's skills directory.
#
# Matches how upstream fulcradynamics/agent-skills says to install (copy the skill
# folders into .claude/skills/). Copy is the default; --symlink is a dev/dogfood
# convenience (a `git pull` then updates the skills in place).
#
# Usage:
#   scripts/coord-setup.sh [--symlink] [--skills-dir DIR] [--engine-only] [--skills-only] [--yes]
#   scripts/coord-setup.sh --uninstall [--skills-dir DIR] [--yes]
#
# Defaults: copy; skills dir = ~/.claude/skills (Claude Code — verified discovery path).
# For another agent, pass --skills-dir (e.g. an OpenClaw skills dir).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SKILLS_SRC="$REPO/skills"
ENGINE_SRC="$REPO/packages/coord-engine"

MODE="copy"; SKILLS_DIR="$HOME/.claude/skills"; YES=0; UNINSTALL=0; DO_ENGINE=1; DO_SKILLS=1
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --symlink) MODE="symlink";;
    --copy) MODE="copy";;
    --skills-dir) SKILLS_DIR="${2:?--skills-dir needs a path}"; shift;;
    --engine-only) DO_SKILLS=0;;
    --skills-only) DO_ENGINE=0;;
    --yes) YES=1;;
    --uninstall) UNINSTALL=1;;
    -h|--help) sed -n '2,18p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
  shift
done

confirm() { [[ "$YES" == "1" ]] && return 0; read -r -p "$1 [y/N] " a || true; [[ "$a" == "y" || "$a" == "Y" ]]; }
skill_names() { find "$SKILLS_SRC" -maxdepth 1 -type d -name 'fulcra-agent-*' -exec basename {} \; | sort; }

if [[ "$UNINSTALL" == "1" ]]; then
  confirm "Uninstall coord: remove skills from ${SKILLS_DIR} + uninstall coord-engine?" || { echo aborted; exit 0; }
  for n in $(skill_names); do rm -rf "${SKILLS_DIR:?}/$n" && echo "removed ${SKILLS_DIR}/$n" || true; done
  command -v uv >/dev/null 2>&1 && uv tool uninstall coord-engine >/dev/null 2>&1 && echo "uninstalled coord-engine" || true
  echo "note: coord leaves a team space's _coord/ sidecar + engine-owned index.md in place; to revert a"
  echo "      team to bare fulcra-agent-teams, delete team/<t>/_coord/ and let an agent re-author task/index.md."
  exit 0
fi

# --- preflight: tools must be present, with actionable messages (not opaque failure) ---
if ! command -v uv >/dev/null 2>&1; then
  echo "error: 'uv' not found. Install it: https://docs.astral.sh/uv/  (curl -LsSf https://astral.sh/uv/install.sh | sh)" >&2
  exit 3
fi
if [[ "$DO_ENGINE" == "1" && ! -d "$ENGINE_SRC" ]]; then
  echo "error: engine/ not found next to this script — run from a coord checkout." >&2; exit 3
fi

if [[ "$DO_ENGINE" == "1" ]]; then
  confirm "Install the coord-engine tool from ${ENGINE_SRC}?" || { echo aborted; exit 0; }
  uv tool install --force "$ENGINE_SRC"
  echo "installed $(coord-engine --help >/dev/null 2>&1 && echo coord-engine || echo 'coord-engine (not on PATH?)')"
fi

if [[ "$DO_SKILLS" == "1" ]]; then
  names="$(skill_names)"; [[ -n "$names" ]] || { echo "error: no fulcra-agent-* skills under $SKILLS_SRC" >&2; exit 3; }
  confirm "Install skills into ${SKILLS_DIR} by ${MODE}? ($(echo "$names" | tr '\n' ' '))" || { echo aborted; exit 0; }
  mkdir -p "$SKILLS_DIR"
  for n in $names; do
    dest="${SKILLS_DIR}/$n"; rm -rf "$dest"
    if [[ "$MODE" == "symlink" ]]; then ln -s "$SKILLS_SRC/$n" "$dest"; else cp -R "$SKILLS_SRC/$n" "$dest"; fi
    echo "  ${MODE}: $n -> $dest"
  done
fi

# --- self-test: prove the real failure modes, not just that a binary resolves ---
echo "self-test:"
if command -v coord-engine >/dev/null 2>&1; then echo "  ✓ coord-engine on PATH"; else echo "  ✗ coord-engine NOT on PATH (add ~/.local/bin?)" >&2; fi
if command -v fulcra-api >/dev/null 2>&1; then echo "  ✓ fulcra-api on PATH (engine shells out to it)"; else
  echo "  ✗ fulcra-api NOT found — the engine needs it. Install + auth: uv tool install fulcra-api && fulcra-api auth login" >&2; fi
if [[ "$DO_SKILLS" == "1" ]]; then
  got="$(ls "$SKILLS_DIR" 2>/dev/null | grep -c '^fulcra-agent-' || true)"
  echo "  ✓ ${got} coord skills present in ${SKILLS_DIR}"
fi
echo "done. Try: coord-engine reconcile <team>   (see skills/fulcra-agent-reconcile)"

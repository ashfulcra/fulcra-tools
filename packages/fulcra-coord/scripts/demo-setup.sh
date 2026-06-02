#!/usr/bin/env bash
#
# demo-setup.sh — per-host one-command readiness for the fulcra-coord
# three-agent coordination demo (#19).
#
# WHY THIS EXISTS
# ---------------
# Standing up the demo on a fresh host is a fiddly multi-step dance: make
# fulcra-coord callable, point it at the demo root, wire the right per-agent
# lifecycle hooks, install the durable inbox listener, and (on exactly one host)
# seed the narrative bus. Doing this by hand per window, under time pressure,
# is where demos break. This script collapses it into one idempotent command you
# run once in each agent's window. Re-running it is safe — every step either
# no-ops if already done or surgically replaces its own managed artifact.
#
# USAGE (run once per agent window)
# ---------------------------------
#   # Claude Code host (laptop): install CC hooks + listener, seed the bus here.
#   scripts/demo-setup.sh --agent-type claude-code --seed
#
#   # ChatGPT-desktop / Codex host: install Codex hooks + listener (no --seed).
#   scripts/demo-setup.sh --agent-type codex
#
#   # OpenClaw host (Mac mini): install OpenClaw hooks + listener.
#   scripts/demo-setup.sh --agent-type openclaw
#
# Seed exactly ONE host (--seed) — it writes the shared narrative state every
# agent then reads. The others just install their own hooks/listener.
#
# OPTIONS
#   --agent-type <claude-code|codex|openclaw>  (required) which adapter to install
#   --seed                  also run scripts/demo_seed.py against the demo root
#   --root <path>           coordination root (default: /coordination-demo)
#   --interval-min <N>      listener cadence in minutes (default: 10)
#   -h | --help             show this help and exit
#
set -euo pipefail

# --- locate the repo so the script works from any CWD ----------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- defaults ---------------------------------------------------------------
AGENT_TYPE=""
DO_SEED=0
ROOT="/coordination-demo"
INTERVAL_MIN=10

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo ""; echo "==> $*"; }

usage() {
  # Print the header comment block (everything between the shebang and `set`).
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; /^set -euo/d'
}

# --- arg parsing ------------------------------------------------------------
while [ "$#" -gt 0 ]; do
  case "$1" in
    --agent-type) AGENT_TYPE="${2:-}"; shift 2 ;;
    --seed)       DO_SEED=1; shift ;;
    --root)       ROOT="${2:-}"; shift 2 ;;
    --interval-min) INTERVAL_MIN="${2:-}"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[ -n "${AGENT_TYPE}" ] || die "--agent-type is required (claude-code|codex|openclaw)"

# Map the agent type to its installer subcommand. Fail loud on a typo rather
# than silently installing the wrong adapter.
case "${AGENT_TYPE}" in
  claude-code) INSTALL_CMD="install-claude-code" ;;
  codex)       INSTALL_CMD="install-codex" ;;
  openclaw)    INSTALL_CMD="install-openclaw" ;;
  *) die "invalid --agent-type '${AGENT_TYPE}' (expected claude-code|codex|openclaw)" ;;
esac

echo "fulcra-coord demo setup"
echo "  agent-type : ${AGENT_TYPE}  (-> ${INSTALL_CMD})"
echo "  root       : ${ROOT}"
echo "  listener   : every ${INTERVAL_MIN} min"
echo "  seed       : $([ "${DO_SEED}" -eq 1 ] && echo yes || echo no)"

# --- (1) ensure fulcra-coord is callable ------------------------------------
# Prefer an already-on-PATH entry point (the common case after a prior run).
# Otherwise try to install from the repo via `uv tool` (preferred) or pip -e,
# and re-check. If it still isn't callable, stop with a clear remediation.
step "(1) Ensuring fulcra-coord is callable"
if command -v fulcra-coord >/dev/null 2>&1; then
  echo "fulcra-coord already on PATH: $(command -v fulcra-coord)"
else
  echo "fulcra-coord not on PATH — attempting an install from ${REPO_ROOT}"
  if command -v uv >/dev/null 2>&1; then
    uv tool install --force "${REPO_ROOT}" || die "uv tool install failed"
  elif command -v pip >/dev/null 2>&1; then
    pip install -e "${REPO_ROOT}" || die "pip install -e failed"
  else
    die "neither 'uv' nor 'pip' found; install one, then re-run (or add fulcra-coord to PATH)"
  fi
  command -v fulcra-coord >/dev/null 2>&1 || die \
    "fulcra-coord still not on PATH after install. Add its bin dir to PATH (e.g. ~/.local/bin or the uv tools bin) and re-run."
fi

# --- (2) point coordination at the demo root + run doctor -------------------
# Export the root for THIS process so every subsequent fulcra-coord call (and
# the seed) targets the demo root, not production /coordination. The echoed
# `export` line is the guidance to put in the operator's shell profile so future
# windows inherit it too.
step "(2) Targeting demo root + running doctor"
export FULCRA_COORD_REMOTE_ROOT="${ROOT}"
echo "Put this in your shell profile so future sessions inherit it:"
echo "    export FULCRA_COORD_REMOTE_ROOT=${ROOT}"
# doctor is advisory here: it checks CLI + remote auth. Don't hard-fail the whole
# setup on a doctor non-zero (the operator may not have auth wired yet) — surface
# it loudly but continue so hooks still install.
fulcra-coord doctor || echo "WARNING: 'fulcra-coord doctor' reported issues — check Fulcra auth before the demo."

# --- (3) install the matching adapter hooks + the durable listener ----------
step "(3) Installing ${INSTALL_CMD} hooks + listener"
fulcra-coord "${INSTALL_CMD}" || die "${INSTALL_CMD} failed"
fulcra-coord install-listener --interval-min "${INTERVAL_MIN}" \
  || die "install-listener failed"

# --- (4) optionally seed the narrative bus (ONE host only) ------------------
if [ "${DO_SEED}" -eq 1 ]; then
  step "(4) Seeding the demo bus at ${ROOT}"
  FULCRA_COORD_REMOTE_ROOT="${ROOT}" python "${REPO_ROOT}/scripts/demo_seed.py" \
    || die "demo_seed.py failed"
else
  echo ""
  echo "(4) Skipping seed (--seed not given). Seed exactly ONE host."
fi

# --- (5) show the seeded mesh so the operator can eyeball it ----------------
step "(5) Current agent mesh"
fulcra-coord agents || echo "WARNING: 'fulcra-coord agents' failed — check remote access."

echo ""
echo "Done. ${AGENT_TYPE} host is ready for the fulcra-coord demo (root ${ROOT})."

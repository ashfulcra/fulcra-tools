#!/usr/bin/env bash
# Pull the latest code and restart the running pieces, so a source checkout
# stays in sync with the repo without re-doing the first-time setup.
#
# What it does, in order:
#   1. git pull --ff-only           — fast-forward to the latest commit
#   2. uv sync --all-packages --all-extras  — re-resolve workspace deps incl. the
#                                     dev (pytest) + macos (PyObjC/rumps) extras.
#                                     Without --all-extras, uv prunes those out,
#                                     re-breaking the menubar import + the tests.
#   3. upgrade the fulcra CLI       — only if installed as a uv tool; a
#                                     kept-current checkout otherwise runs
#                                     whatever CLI it first installed forever
#   4. restart the launchd daemon   — only if it's installed; picks up new code
#   5. restart the menubar app      — only if it's currently running
#
# Steps 3–5 are best-effort: if you run the daemon in the foreground
# (`uv run fulcra-collect daemon`) or don't use the menubar, those steps are
# skipped with a note rather than failing.
#
# Run from anywhere:  bash scripts/update.sh
# Requires: git, uv. macOS for the launchd/menubar steps.
set -euo pipefail
cd "$(dirname "$0")/.."                        # repo root
REPO="$PWD"

echo "=== 1/5  git pull --ff-only ==="
git pull --ff-only

echo "=== 2/5  uv sync --all-packages --all-extras ==="
uv sync --all-packages --all-extras

echo "=== 3/5  upgrade the fulcra CLI ==="
# Newer CLI command groups (file, data-updates, data-type, tag) are load-
# bearing for collect + fulcra-coord; a checkout that stays current via this
# script previously never upgraded the CLI it installed on day one.
if uv tool list 2>/dev/null | grep -q '^fulcra-api '; then
  if uv tool install --upgrade fulcra-api >/dev/null 2>&1; then
    echo "  fulcra-api: $(uv tool list 2>/dev/null | grep '^fulcra-api ' | head -1)"
  else
    echo "  warning: fulcra-api upgrade failed — continuing (daemon still restarts)"
  fi
else
  echo "  fulcra-api not installed as a uv tool — skipping (scripts/setup.sh installs it)"
fi

LABEL="com.fulcra.collect"
echo "=== 4/5  restart the launchd daemon ($LABEL) ==="
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  # -k sends SIGKILL and relaunches, so the fresh process imports the new code.
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  echo "  kickstarted $LABEL"
else
  echo "  $LABEL is not loaded — skipping (run it in the foreground with"
  echo "  'uv run fulcra-collect daemon', or install it per docs/TESTING.md)."
fi

echo "=== 5/5  restart the menubar app ==="
if pgrep -f fulcra-menubar >/dev/null 2>&1; then
  pkill -f fulcra-menubar || true
  sleep 1
  # Relaunch detached so it outlives this script.
  if command -v fulcra-menubar >/dev/null 2>&1; then
    nohup fulcra-menubar >/tmp/fulcra-menubar.log 2>&1 &
  else
    nohup uv run --package fulcra-menubar python -m fulcra_menubar \
      >/tmp/fulcra-menubar.log 2>&1 &
  fi
  disown || true
  echo "  relaunched fulcra-menubar (log: /tmp/fulcra-menubar.log)"
else
  echo "  menubar not running — skipping (start it with"
  echo "  'uv run --package fulcra-menubar python -m fulcra_menubar')."
fi

echo "Done. Now on $(git rev-parse --short HEAD): $(git log -1 --pretty=%s)"

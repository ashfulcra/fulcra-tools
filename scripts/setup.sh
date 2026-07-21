#!/usr/bin/env bash
# Fulcra Collect — one-command first-time setup on macOS.
#
# What it does, in order:
#   1. Prereqs: confirm `brew`, install/update `uv`, install Python 3.12.
#   2. Workspace deps: `uv sync --all-packages --all-extras` (bare `uv sync`
#      DOESN'T install the dev/macos extras → tests fail + menubar can't import).
#   3. Fulcra CLI: `uv tool install fulcra-api` (used by browser sign-in +
#      token refresh; idempotent).
#   4. Test suite: `uv run pytest packages/ -q` (the full test suite, must NOT hit the
#      network — if it does, that's the bug, not slowness).
#   5. Print the next steps for actually running the app.
#
# This script is idempotent — safe to re-run. It DOES NOT start the daemon,
# install the launchd agent, or open a browser; those are explicit steps
# under the user's control. See `docs/TESTING.md` for the full first-run flow.
#
# Run from the repo root:  bash scripts/setup.sh
# Requires macOS (the menubar's PyObjC deps are macOS-only). Linux contributors
# can still run steps 2-4 by skipping step 1's brew check.

set -euo pipefail
cd "$(dirname "$0")/.."                        # repo root
REPO="$PWD"

bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }

have()   { command -v "$1" >/dev/null 2>&1; }

bold "=== 1/5  Prereqs ==="
if [ "$(uname)" = "Darwin" ]; then
  if ! have brew; then
    red "Homebrew not found."
    echo "  Install it from https://brew.sh, then re-run this script."
    exit 1
  fi
  if ! have uv; then
    echo "  installing uv via brew…"
    brew install uv
  else
    echo "  uv: $(uv --version)"
  fi
  if ! brew list --formula python@3.12 >/dev/null 2>&1; then
    echo "  installing python@3.12 via brew…"
    brew install python@3.12
  else
    echo "  python@3.12 already installed"
  fi
else
  yellow "  non-macOS host — skipping brew/python check (manual install required)"
  have uv || { red "  uv not found — install it from https://docs.astral.sh/uv/"; exit 1; }
fi

bold "=== 2/5  Workspace sync (--all-packages --all-extras) ==="
uv sync --all-packages --all-extras

bold "=== 3/5  Fulcra CLI ==="
# Install-or-upgrade unconditionally: the old "skip if present" check
# stranded machines on whatever CLI version they first installed, silently
# missing newer command groups (file, data-updates, data-type, tag) that
# Collect and coord integrations depend on. `uv tool install --upgrade` is
# idempotent and a no-op when already current.
echo "  installing/upgrading fulcra-api uv tool…"
uv tool install --upgrade fulcra-api
have fulcra && echo "  fulcra: $(command -v fulcra)" || \
  yellow "  warning: \`fulcra\` not on PATH — add ~/.local/bin to PATH or use the launchd agent's bundled PATH"

bold "=== 4/5  Test suite ==="
uv run pytest packages/ -q
green "  test suite green"

bold "=== 5/5  Done. Next steps ==="
cat <<'NEXT'

  Start the daemon (foreground, for first-run / dev):
    uv run fulcra-collect daemon

  Or install + start the persistent launchd agent:
    uv run fulcra-collect install
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fulcra.collect.plist

  Open the onboarding wizard (after the daemon is up):
    open "$(cat ~/.config/fulcra-collect/web-url)"

  Diagnose problems any time:
    uv run fulcra-collect doctor

  Keep the checkout in sync later:
    bash scripts/update.sh

  Full first-run walkthrough + troubleshooting: docs/TESTING.md
NEXT

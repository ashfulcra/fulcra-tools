#!/usr/bin/env bash
# Build the self-contained "Fulcra Collect.app" with Briefcase.
#
# Briefcase installs the app's requirements with pip `--only-binary :all:`
# (wheels only), so two kinds of dependency need a local wheel first:
#   1. the monorepo's own workspace packages (not on PyPI), and
#   2. pure-python deps published sdist-only (rumps, sgmllib3k).
# We build all of those into ./wheelhouse and point pip at it via
# PIP_FIND_LINKS. Everything else (fastapi, uvicorn, pyobjc, pydantic-core,
# …) resolves from PyPI as normal wheels.
#
# Run from the repo root:  bash packages/menubar/scripts/build_macos_app.sh
# Requires: uv, a py2app-free Python 3.13 (Briefcase doesn't support 3.14 yet).
set -euo pipefail
cd "$(dirname "$0")/../../.."                 # repo root
REPO="$PWD"
WHEELHOUSE="$REPO/wheelhouse"

echo "=== 1/3  build workspace wheels into wheelhouse/ ==="
rm -rf "$WHEELHOUSE"; mkdir -p "$WHEELHOUSE"
for pkg in fulcra-common fulcra-collect fulcra-media-helpers \
           fulcra-dayone fulcra-attention fulcra-csv-importer; do
  uv build --package "$pkg" --wheel --out-dir "$WHEELHOUSE" >/dev/null
done

echo "=== 2/3  build sdist-only pure-python deps into wheelhouse/ ==="
# rumps + sgmllib3k ship as sdists only; --only-binary :all: would reject them.
uvx pip wheel "rumps>=0.4" sgmllib3k --no-deps -w "$WHEELHOUSE" >/dev/null
ls "$WHEELHOUSE"

echo "=== 3/3  briefcase create + build (PIP_FIND_LINKS=wheelhouse) ==="
cd "$REPO/packages/menubar"
rm -rf build dist
PIP_FIND_LINKS="$WHEELHOUSE" uvx briefcase create macOS
PIP_FIND_LINKS="$WHEELHOUSE" uvx briefcase build macOS

echo "Built: packages/menubar/build/fulcra-menubar/macos/app/Fulcra Collect.app"
echo "Verify with: bash packages/menubar/scripts/verify_bundle.sh"

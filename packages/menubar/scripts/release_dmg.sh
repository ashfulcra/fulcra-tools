#!/usr/bin/env bash
# Build a SIGNED + NOTARIZED "Fulcra Collect.dmg" for beta distribution.
#
# The .dmg contains three things a beta tester needs:
#   1. Fulcra Collect.app         — the daemon + menubar (collect core + every
#                                    plugin, gmail included), signed inside-out
#                                    by Briefcase with a Developer ID identity
#                                    and the hardened-runtime entitlements a
#                                    Python app requires.
#   2. Fulcra Attention Extension — the built Chrome extension (dist/), loaded
#                                    unpacked per INSTALL.txt.
#   3. INSTALL.txt                — drag-to-Applications + load-the-extension +
#                                    sign-in instructions.
# ...plus an /Applications symlink for drag-install.
#
# Why a custom .dmg instead of `briefcase package -p dmg`: Briefcase's dmg holds
# only the app + Applications symlink. We need the extension + instructions to
# ride along, so we let Briefcase SIGN the app (it knows the bundle's nested
# binaries and applies the right entitlements), then assemble + sign + notarize
# our own .dmg around it.
#
# PREREQUISITES (yours — this script does not create secrets):
#   * A "Developer ID Application" certificate in the login keychain. Check:
#       security find-identity -v -p codesigning | grep "Developer ID Application"
#     (An "Apple Development" cert is NOT sufficient — it cannot be notarized
#      for distribution.)
#   * A stored notarytool credential profile. Create once:
#       xcrun notarytool store-credentials "$FULCRA_NOTARY_PROFILE" \
#         --apple-id <apple-id> --team-id <TEAMID> --password <app-specific-pw>
#
# USAGE:
#   FULCRA_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#   FULCRA_NOTARY_PROFILE="fulcra-notary" \
#   bash packages/menubar/scripts/release_dmg.sh
#
# Env:
#   FULCRA_SIGN_IDENTITY   (required) the Developer ID Application identity —
#                          full name or the 40-hex fingerprint.
#   FULCRA_NOTARY_PROFILE  (default "fulcra-notary") notarytool keychain profile.
#   FULCRA_SKIP_NOTARIZE   (optional) set to 1 to sign + build the dmg but skip
#                          notarization (for a local signing smoke test).
set -euo pipefail

: "${FULCRA_SIGN_IDENTITY:?set FULCRA_SIGN_IDENTITY to your 'Developer ID Application: …' identity}"
NOTARY_PROFILE="${FULCRA_NOTARY_PROFILE:-fulcra-notary}"

cd "$(dirname "$0")/../../.."                 # repo root
REPO="$PWD"
MENUBAR="$REPO/packages/menubar"
APP_REL="build/fulcra-menubar/macos/app/Fulcra Collect.app"
APP="$MENUBAR/$APP_REL"
EXT_SRC="$REPO/packages/attention/chrome"
OUT_DMG="$REPO/dist/Fulcra Collect.dmg"

echo "=== 1/7  build the unsigned app (wheelhouse + briefcase create/build) ==="
bash "$MENUBAR/scripts/build_macos_app.sh"

echo "=== 2/7  sign the app inside-out via Briefcase (Developer ID, hardened runtime) ==="
# `package -p zip … --no-notarize` runs Briefcase's signer over every nested
# Mach-O with the macOS template's Python entitlements, then zips the signed
# app. We only want the side effect: build/…/Fulcra Collect.app signed in place.
cd "$MENUBAR"
PIP_FIND_LINKS="$REPO/wheelhouse" uvx briefcase package macOS \
  -p zip -i "$FULCRA_SIGN_IDENTITY" --no-notarize
cd "$REPO"
echo "--- verify app signature ---"
codesign --verify --deep --strict --verbose=2 "$APP"
codesign -dv --verbose=4 "$APP" 2>&1 | grep -iE "Authority|TeamIdentifier|Timestamp|flags" | head

echo "=== 3/7  build the attention extension (dist/) ==="
( cd "$EXT_SRC" && npm ci --no-fund --no-audit >/dev/null 2>&1 && npm run build >/dev/null )
test -f "$EXT_SRC/dist/manifest.json" || { echo "ERROR: extension dist/ not built" >&2; exit 1; }

echo "=== 4/7  stage .dmg contents ==="
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/Fulcra Collect.app"
cp -R "$EXT_SRC/dist" "$STAGE/Fulcra Attention Extension"
cp "$MENUBAR/resources/dmg/INSTALL.txt" "$STAGE/INSTALL.txt"
ln -s /Applications "$STAGE/Applications"

echo "=== 5/7  create the .dmg ==="
mkdir -p "$REPO/dist"
rm -f "$OUT_DMG"
hdiutil create -volname "Fulcra Collect" -srcfolder "$STAGE" \
  -ov -format UDZO "$OUT_DMG"

echo "=== 6/7  sign the .dmg ==="
codesign --force --timestamp -s "$FULCRA_SIGN_IDENTITY" "$OUT_DMG"
codesign --verify --verbose=2 "$OUT_DMG"

if [ "${FULCRA_SKIP_NOTARIZE:-0}" = "1" ]; then
  echo "=== 7/7  SKIPPED notarization (FULCRA_SKIP_NOTARIZE=1) ==="
  echo "Built (signed, NOT notarized): $OUT_DMG"
  echo "Note: unnotarized dmgs trip Gatekeeper on other machines."
  exit 0
fi

echo "=== 7/7  notarize + staple (profile: $NOTARY_PROFILE) ==="
xcrun notarytool submit "$OUT_DMG" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$OUT_DMG"
echo "--- Gatekeeper assessment ---"
spctl -a -t open --context context:primary-signature -v "$OUT_DMG" || true
xcrun stapler validate "$OUT_DMG"

echo "Built (signed + notarized + stapled): $OUT_DMG"

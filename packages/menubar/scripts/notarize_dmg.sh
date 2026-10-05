#!/usr/bin/env bash
# Submit a signed dmg to Apple and quarantine it if any Apple gate fails.
set -u -o pipefail

DMG="${1:?usage: notarize_dmg.sh <dmg> <keychain-profile>}"
PROFILE="${2:?usage: notarize_dmg.sh <dmg> <keychain-profile>}"
QUARANTINED="${DMG%.dmg}.NOT_NOTARIZED.dmg"

quarantine() {
  if [ -f "$DMG" ]; then
    mv -f "$DMG" "$QUARANTINED"
  fi
  echo "ERROR: Apple notarization or stapling failed." >&2
  echo "NOT DISTRIBUTABLE: retained at $QUARANTINED" >&2
  exit 1
}

xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait || quarantine
xcrun stapler staple "$DMG" || quarantine
xcrun stapler validate "$DMG" || quarantine

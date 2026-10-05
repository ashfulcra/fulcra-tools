#!/usr/bin/env bash
# Submit a signed dmg to Apple and quarantine it if any Apple gate fails.
set -u -o pipefail

DMG="${1:?usage: notarize_dmg.sh <dmg> <keychain-profile>}"
PROFILE="${2:?usage: notarize_dmg.sh <dmg> <keychain-profile>}"
QUARANTINED="${DMG%.dmg}.NOT_NOTARIZED.dmg"

quarantine() {
  echo "ERROR: Apple notarization or stapling failed." >&2
  if [ ! -e "$DMG" ]; then
    echo "NOT DISTRIBUTABLE: no artifact remains at the release path." >&2
    exit 1
  fi
  if mv -f "$DMG" "$QUARANTINED"; then
    echo "NOT DISTRIBUTABLE: retained at $QUARANTINED" >&2
    exit 1
  fi
  if [ ! -e "$DMG" ] && [ -f "$QUARANTINED" ]; then
    echo "NOT DISTRIBUTABLE: retained at $QUARANTINED" >&2
    exit 1
  fi
  echo "ERROR: could not move the artifact to quarantine." >&2
  if rm -f "$DMG" && [ ! -e "$DMG" ]; then
    echo "NOT DISTRIBUTABLE: canonical artifact deleted instead." >&2
    exit 1
  fi
  echo "CRITICAL: NOT DISTRIBUTABLE artifact still exists at $DMG" >&2
  exit 1
}

xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait || quarantine
xcrun stapler staple "$DMG" || quarantine
xcrun stapler validate "$DMG" || quarantine

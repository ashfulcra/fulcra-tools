#!/usr/bin/env bash
# The release gate: assess a signed + notarized dmg and decide if it ships.
#
# Split out of release_dmg.sh so the decision — the part with the accidental-ship
# risk — is exercisable in isolation with a stubbed `spctl`
# (see packages/menubar/tests/test_gatekeeper_gate.py).
#
# Why this exists: a dmg can be signed, notarized AND stapled and still be
# REJECTED by Gatekeeper (signing or policy problems). A rejected dmg is one a
# beta tester cannot open normally, so a rejection is a build FAILURE.
#
# The retention rule is the point: a rejected image must NEVER be left at the
# canonical release path, because that is exactly where a good release appears
# and where a human (or a script) reaches for something to ship.
#
#   accepted                  → print the success line, exit 0, artifact stays.
#   rejected (default)        → DELETE the artifact, exit 1. Nothing shippable
#                               is left behind.
#   rejected + override=1     → MOVE the artifact to <name>.REJECTED.dmg and
#                               exit 1. Retained for diagnosis, but not at the
#                               release path and unmistakably named.
#
# The success line is printed on exactly one path: an accepted assessment.
#
# Usage:   bash gatekeeper_gate.sh <path-to-dmg>
# Env:     FULCRA_ALLOW_GATEKEEPER_FAIL=1  retain a rejected artifact (quarantined)
set -euo pipefail

DMG="${1:?usage: gatekeeper_gate.sh <path-to-dmg>}"

echo "--- Gatekeeper assessment ---"
if spctl -a -t open --context context:primary-signature -v "$DMG"; then
  echo "Built (signed + notarized + stapled): $DMG"
  exit 0
fi

REJECTED="${DMG%.dmg}.REJECTED.dmg"

if [ "${FULCRA_ALLOW_GATEKEEPER_FAIL:-0}" = "1" ]; then
  mv -f "$DMG" "$REJECTED"
  {
    echo ""
    echo "NOT DISTRIBUTABLE: Gatekeeper REJECTED the image."
    echo "Retained for diagnosis at: $REJECTED"
    echo "  (FULCRA_ALLOW_GATEKEEPER_FAIL=1). It has been moved OFF the release"
    echo "  path so it cannot be shipped by mistake. Beta testers could not open"
    echo "  it. Do not distribute it."
    echo "Inspect: spctl -a -t open --context context:primary-signature -vv \"$REJECTED\""
  } >&2
  exit 1
fi

rm -f "$DMG"
{
  echo ""
  echo "ERROR: Gatekeeper REJECTED the image — it is signed and notarized but"
  echo "       would not open normally on a beta tester's machine."
  echo "       Removed $DMG so a rejected build can never be mistaken for a"
  echo "       release."
  echo "       Re-run with FULCRA_ALLOW_GATEKEEPER_FAIL=1 to keep the artifact"
  echo "       (quarantined) for diagnosis, then inspect with:"
  echo "         spctl -a -t open --context context:primary-signature -vv <dmg>"
  echo "         xcrun notarytool log <submission-id> --keychain-profile <profile>"
} >&2
exit 1

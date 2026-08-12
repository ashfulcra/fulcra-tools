#!/usr/bin/env bash
# Run the Safari app's Swift XCTest suites — WITHOUT the Xcode project.
#
# WHY THIS EXISTS
# ---------------
# `FulcraAttention.xcodeproj` has no test target. Every .swift file is wired to a
# target explicitly (no synchronized folders), and the test files are wired to
# none — so `EnsureDefinitionTests.swift` has never compiled or run since it
# landed. The team already noticed and worked around it by writing standalone
# `@main` parity scripts (WireParityCheck, EnsureDefinitionParityCheck), which
# DO run but only when someone remembers to invoke them by hand.
#
# Separately, no CI workflow invokes `xcodebuild` at all, so nothing in this
# directory has ever been compiled by CI.
#
# This script closes both gaps the cheap way: it assembles the platform-agnostic
# sources plus the XCTest files into a throwaway SwiftPM package and runs
# `swift test`. No Xcode project, no code signing, no provisioning profile — so
# it works headlessly and in CI, which an `xcodebuild test` of a signed app
# target does not (see the App Group / Keychain capability note in
# docs/proposals/2026-06-04-relayless-and-mobile-safari-attention.md).
#
# It is a stopgap, not the destination: a real test target in the xcodeproj is
# still the right fix, and it is the only way to test the parts that genuinely
# need the app sandbox (Keychain access groups, App Group container).
#
# Usage:  bash packages/attention/safari/scripts/run_swift_tests.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/../FulcraAttention/macOS (App)"
EXT="$HERE/../FulcraAttention/Shared (Extension)"
TESTS="$HERE/../FulcraAttention/FulcraAttentionTests"

# Platform-agnostic logic only. Deliberately excluded:
#   AppDelegate / SignInView / ViewController — AppKit UI, nothing to unit test;
#   *ParityCheck.swift — standalone `@main` executables; `@main` is illegal in a
#   test target and they are already runnable on their own.
SOURCES=(
  "Wire.swift"
  "EnsureDefinition.swift"
  "KeychainStore.swift"
  "Sharing.swift"
  "DeviceIdentity.swift"
  "AuthManager.swift"
  "SentSet.swift"
  "Ingest.swift"
)

# Platform-agnostic sources from the shared EXTENSION folder. Excluded:
# SafariWebExtensionHandler.swift — it subclasses an NSExtension host type and
# is a thin adapter by design; everything decidable lives in NativeBridge.
EXT_SOURCES=(
  "NativeBridge.swift"
)

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PKG="$WORK/FulcraAttentionCore"
mkdir -p "$PKG/Sources/FulcraAttention" "$PKG/Tests/FulcraAttentionTests"

# The module is named FulcraAttention because the existing test files already say
# `@testable import FulcraAttention` — matching the Xcode app target's module
# name. Naming it anything else would mean editing their files to suit the
# harness, which is backwards.
for f in "${SOURCES[@]}"; do
  [ -f "$APP/$f" ] || { echo "ERROR: missing source $APP/$f" >&2; exit 1; }
  cp "$APP/$f" "$PKG/Sources/FulcraAttention/$f"
done
for f in "${EXT_SOURCES[@]}"; do
  [ -f "$EXT/$f" ] || { echo "ERROR: missing source $EXT/$f" >&2; exit 1; }
  cp "$EXT/$f" "$PKG/Sources/FulcraAttention/$f"
done

shopt -s nullglob
test_files=("$TESTS"/*Tests.swift)
shopt -u nullglob
if [ ${#test_files[@]} -eq 0 ]; then
  echo "ERROR: no *Tests.swift found in $TESTS — refusing to report success" >&2
  exit 1
fi
for f in "${test_files[@]}"; do
  cp "$f" "$PKG/Tests/FulcraAttentionTests/$(basename "$f")"
done

cat > "$PKG/Package.swift" <<'SWIFT'
// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "FulcraAttentionCore",
    platforms: [.macOS(.v13)],
    targets: [
        .target(name: "FulcraAttention"),
        .testTarget(name: "FulcraAttentionTests", dependencies: ["FulcraAttention"]),
    ]
)
SWIFT

echo "=== running $(basename "${test_files[0]}") + $(( ${#test_files[@]} - 1 )) other test file(s) ==="
cd "$PKG"
swift test 2>&1 | tail -40

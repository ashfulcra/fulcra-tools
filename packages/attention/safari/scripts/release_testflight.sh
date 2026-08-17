#!/usr/bin/env bash
# Build, sign, export and upload the iOS Fulcra Attention app to TestFlight.
#
# WHY THIS EXISTS
# ---------------
# Everything up to signing is already reproducible and green — `xcode.yml`
# builds all four targets on every change, and the archive step below has been
# verified end to end with signing disabled (ARCHIVE SUCCEEDED, extension
# correctly embedded with the Safari bundle inside it). What was missing was a
# path from "the project archives" to "a build is in TestFlight", and that path
# is entirely about credentials the CI runner must never hold.
#
# So this script is deliberately split: it does every mechanical step itself,
# and it FAILS LOUDLY AND EARLY on each credential it cannot supply, naming the
# exact thing a human has to do. It never guesses, and it never half-uploads.
#
# WHAT YOU NEED BEFORE THIS WORKS (see the preflight below — it checks each one)
#   1. An "Apple Distribution" certificate in the login keychain.
#      As of the last check this Mac has only "Apple Development" and
#      "Developer ID Application". Developer ID signs a notarised .dmg for
#      direct download; it CANNOT be used for TestFlight. Different cert.
#   2. An app record in App Store Connect for bundle id com.fulcra.attention.
#   3. An App Store Connect API key (.p8) so the upload runs unattended.
#
# Usage:
#   export ASC_KEY_ID=XXXXXXXXXX ASC_ISSUER_ID=xxxxxxxx-xxxx-...
#   bash packages/attention/safari/scripts/release_testflight.sh [--dry-run]
#
# --dry-run runs preflight + archive + export and stops before upload, which is
# the useful mode for proving the pipeline without publishing anything.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$HERE/../FulcraAttention/FulcraAttention.xcodeproj"
CHROME="$HERE/../../chrome"
SCHEME="FulcraAttention (iOS)"
BUNDLE_ID="com.fulcra.attention"
TEAM_ID="CWH48N2H7F"
WORK="${TMPDIR:-/tmp}/fulcra-attention-testflight"
ARCHIVE="$WORK/FulcraAttention-iOS.xcarchive"
EXPORT_DIR="$WORK/export"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mBLOCKED: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
# Every check here failed for a real reason at some point, and each one costs
# seconds now versus a confusing failure ten minutes into an archive.
say "Preflight"

xcrun --find altool >/dev/null 2>&1 || die "altool not found — install/select Xcode (xcode-select -p)."

if ! security find-identity -v -p codesigning | grep -q "Apple Distribution"; then
  die "no 'Apple Distribution' certificate in the keychain.

  This is the single hard blocker and it needs Ash's Apple ID.
  In Xcode: Settings -> Accounts -> (Apple ID) -> Manage Certificates
            -> + -> Apple Distribution.
  'Developer ID Application' is NOT a substitute: that one signs a notarised
  .dmg for direct download, not an App Store / TestFlight build."
fi

: "${ASC_KEY_ID:?set ASC_KEY_ID (App Store Connect API key id, 10 chars)}"
: "${ASC_ISSUER_ID:?set ASC_ISSUER_ID (App Store Connect issuer UUID)}"
if ! ls "$HOME/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID}.p8" >/dev/null 2>&1; then
  die "API key file not found at ~/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID}.p8
  Download it once from App Store Connect -> Users and Access -> Integrations -> Keys.
  Apple lets you download a key EXACTLY ONCE; if it is lost, revoke and make a new one."
fi

command -v npm >/dev/null || die "npm not found — needed to build the extension bundles."

# ------------------------------------------------------------------ bundles
# BOTH bundles, always. The extension target copies a built bundle as
# resources, so a stale or missing dist produces an .appex that installs and
# then does nothing — a silent failure, not a build error.
say "Building extension bundles (chrome + safari)"
( cd "$CHROME" && npm ci --silent && npm run build && npx vite build --config vite.safari.config.ts )

# ------------------------------------------------------------------ archive
say "Archiving $SCHEME"
rm -rf "$WORK"; mkdir -p "$WORK"
xcodebuild -project "$PROJ" -scheme "$SCHEME" -sdk iphoneos -configuration Release \
  -archivePath "$ARCHIVE" \
  DEVELOPMENT_TEAM="$TEAM_ID" \
  archive

# Prove the payload is right BEFORE uploading. A missing .appex or an empty
# extension bundle is invisible in the xcodebuild output and expensive to find
# after the fact — App Store Connect accepts the build and the extension simply
# never appears in Safari's settings.
APP="$ARCHIVE/Products/Applications/FulcraAttention.app"
APPEX="$APP/PlugIns/FulcraAttention Extension.appex"
[ -d "$APPEX" ] || die "the archive has no embedded extension at PlugIns/ — the app would ship without the Safari extension."
[ -f "$APPEX/manifest.json" ] || die "the extension has no manifest.json — the Safari bundle did not get copied in."
say "Archive payload OK ($(find "$APPEX" -name '*.js' | wc -l | tr -d ' ') js files, manifest present)"

# ------------------------------------------------------------------- export
say "Exporting for App Store Connect"
cat > "$WORK/ExportOptions.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>method</key><string>app-store-connect</string>
  <key>teamID</key><string>$TEAM_ID</string>
  <key>uploadSymbols</key><true/>
  <!-- Let Xcode fetch/renew the App Store provisioning profiles for BOTH the
       app and the extension. Managing them by hand means two profiles to keep
       in sync, and a stale extension profile fails at export with a message
       that points at the app. -->
  <key>signingStyle</key><string>automatic</string>
  <key>destination</key><string>export</string>
</dict>
</plist>
PLIST

xcodebuild -exportArchive -archivePath "$ARCHIVE" \
  -exportPath "$EXPORT_DIR" -exportOptionsPlist "$WORK/ExportOptions.plist"

IPA="$(find "$EXPORT_DIR" -name '*.ipa' | head -1)"
[ -n "$IPA" ] || die "export produced no .ipa in $EXPORT_DIR"
say "Exported $(basename "$IPA") ($(du -h "$IPA" | cut -f1))"

if [ "$DRY_RUN" = "1" ]; then
  say "--dry-run: stopping before upload. The .ipa is at:"
  echo "  $IPA"
  exit 0
fi

# ------------------------------------------------------------------- upload
# Validate first. `altool --validate-app` runs the same checks the upload does
# but changes nothing, so a rejection costs one round trip instead of a
# half-published build and a burned build number.
say "Validating with App Store Connect"
xcrun altool --validate-app -f "$IPA" -t ios \
  --apiKey "$ASC_KEY_ID" --apiIssuer "$ASC_ISSUER_ID"

say "Uploading to TestFlight"
xcrun altool --upload-app -f "$IPA" -t ios \
  --apiKey "$ASC_KEY_ID" --apiIssuer "$ASC_ISSUER_ID"

say "Uploaded. Processing in App Store Connect usually takes 5-15 minutes."
echo "  Watch: https://appstoreconnect.apple.com/apps -> TestFlight"
echo "  NOTE: the build number ($(defaults read "$APP/Info" CFBundleVersion 2>/dev/null || echo '?')) must increase on every upload;"
echo "        App Store Connect rejects a repeat. Bump CURRENT_PROJECT_VERSION."

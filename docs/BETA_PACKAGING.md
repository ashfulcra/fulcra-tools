# Beta packaging — signed macOS `.dmg`

How to produce the signed, notarized **Fulcra Collect.dmg** that beta testers
install. The `.dmg` bundles the app (collect core + every plugin, gmail
included), the built attention Chrome extension, and install instructions.

## What's automated vs. what's yours

`packages/menubar/scripts/release_dmg.sh` does the whole build → sign →
assemble → notarize → staple flow. It needs two things that only you can set up,
because they involve your Apple Developer account and secrets the script never
handles:

1. **A "Developer ID Application" certificate** in the login keychain.
2. **A stored notarization credential** (a `notarytool` keychain profile).

An "Apple Development" certificate is **not** sufficient — it signs for local
testing but cannot be notarized for distribution.

## One-time setup

### 1. Developer ID Application certificate

Create it in your Apple Developer account (you must be Account Holder or Admin):
Certificates, Identifiers & Profiles → Certificates → **+** → **Developer ID
Application** → follow the CSR steps → download and double-click to install into
the login keychain. Verify:

```
security find-identity -v -p codesigning | grep "Developer ID Application"
```

You want a line like `Developer ID Application: Your Name (TEAMID)`.

### 2. Notarization credential profile

Create an app-specific password at appleid.apple.com → Sign-In & Security →
App-Specific Passwords. Then store a reusable notarytool profile:

```
xcrun notarytool store-credentials "fulcra-notary" \
  --apple-id "<your-apple-id>" \
  --team-id "<TEAMID>" \
  --password "<app-specific-password>"
```

(Alternatively use an App Store Connect API key; pass `--key`/`--key-id`/
`--issuer` to `store-credentials` instead.)

## Build a release

```
FULCRA_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
FULCRA_NOTARY_PROFILE="fulcra-notary" \
bash packages/menubar/scripts/release_dmg.sh
```

Output: `dist/Fulcra Collect.dmg`, signed + notarized + stapled.

Environment variables:

| var | required | default | meaning |
|---|---|---|---|
| `FULCRA_SIGN_IDENTITY` | yes | — | Developer ID Application identity (full name or 40-hex fingerprint). |
| `FULCRA_NOTARY_PROFILE` | no | `fulcra-notary` | notarytool keychain profile name. |
| `FULCRA_SKIP_NOTARIZE` | no | `0` | set `1` to sign + build the dmg but skip notarization (local signing smoke test). |
| `FULCRA_ALLOW_GATEKEEPER_FAIL` | no | `0` | set `1` to retain a Gatekeeper-rejected dmg for diagnosis — quarantined to `<name>.REJECTED.dmg`, never left at the release path. Default: the rejected dmg is deleted. Either way the run exits non-zero and never reports success. |

## What the script does

1. Builds the unsigned app (`build_macos_app.sh`: workspace wheels → Briefcase
   create/build; fails loudly if collect/gmail/common aren't bundled).
2. Signs the app inside-out with Briefcase (`package -p zip -i … --no-notarize`)
   — Briefcase applies the hardened-runtime entitlements a Python app needs and
   signs every nested Mach-O; then verifies with `codesign --verify --deep`.
3. Builds the attention extension (`npm ci && npm run build`).
4. Stages the app + `Fulcra Attention Extension/` + `INSTALL.txt` + an
   `/Applications` symlink.
5. Creates the `.dmg` (`hdiutil`).
6. Signs the `.dmg` (`codesign --timestamp`).
7. Notarizes (`notarytool submit --wait`), staples (`stapler staple`), and
   validates the staple.
8. Runs the Gatekeeper assessment (`spctl`, in `gatekeeper_gate.sh`) as the
   **release gate**. A dmg can be notarized and stapled and still be rejected,
   and a rejected dmg is one a beta tester cannot open — so a rejection fails
   the build loudly and, crucially, does **not** leave a rejected image at the
   release path: by default it is deleted, or with
   `FULCRA_ALLOW_GATEKEEPER_FAIL=1` moved to `<name>.REJECTED.dmg`. The success
   line prints only after an accepted assessment.

## Verify a built dmg

```
xcrun stapler validate "dist/Fulcra Collect.dmg"
spctl -a -t open --context context:primary-signature -v "dist/Fulcra Collect.dmg"
```

## Troubleshooting

- **`security find-identity` shows only "Apple Development"** — the Developer ID
  Application cert isn't installed; redo one-time step 1.
- **`notarytool` auth error** — the profile is missing or the app-specific
  password was rotated; recreate it (one-time step 2).
- **Notarization "Invalid" with signing errors** — usually a nested binary
  signed without the hardened runtime. The script signs via Briefcase precisely
  to avoid this; if it recurs, fetch the log with
  `xcrun notarytool log <submission-id> --keychain-profile fulcra-notary`.
- **`app_packages` empty / plugin ImportError** — the app build failed a
  dependency resolution; see `packages/menubar/logs/briefcase.*.create.log`.
  (`build_macos_app.sh` now fails loudly on this instead of reporting success.)

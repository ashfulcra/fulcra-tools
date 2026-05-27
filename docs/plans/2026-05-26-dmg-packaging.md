# Sprint C scope: `.dmg` packaging for one-click install

**Task:** #63
**Author:** scoped 2026-05-26 (late)
**Status:** plan-only — code in a future session

## Goal

End-user downloads `Fulcra Collect.dmg`, drags `Fulcra Collect.app` into `/Applications`, opens it. The menubar app appears, and behind the scenes the collect daemon is registered via SMAppService so it autostarts on every login. Zero terminal commands. No `brew install`, no `uv tool install`, no `launchctl load`.

Today's install path is 3 commands assuming the user has `uv` + a git clone + the mental model of `launchctl`. **Not consumer software.** Sprint A (daemon lifecycle from the menubar) removed `launchctl` from the *ongoing* path; this sprint removes `uv tool install` and the git-clone requirement from the *initial* path.

## State of the foundation

Two big things already in place that make this tractable:

1. **`py2app>=0.28`** is already declared as a `build` extra in `packages/menubar/pyproject.toml`. Nobody's run it yet but the dep is here.
2. **`SMAppService` integration** landed in Sprint A — the menubar can register/unregister the daemon as a login item from inside the app bundle, which is exactly what we need for the .dmg path.

The unknowns are all in the packaging mechanics: bundling, code-signing, notarization, binary deps that don't play nicely with py2app.

## What the .app needs to contain

```
Fulcra Collect.app/
├── Contents/
│   ├── Info.plist                          ← LSUIElement=true (no Dock icon)
│   ├── MacOS/
│   │   └── Fulcra Collect                  ← py2app entry → fulcra_menubar.__main__:main
│   └── Resources/
│       ├── menubar-icon.png
│       ├── lib/                            ← bundled Python + deps
│       │   └── python3.13/
│       │       └── site-packages/
│       │           ├── fulcra_menubar/
│       │           ├── fulcra_collect/      ← daemon + CLI lives in here too
│       │           ├── fulcra_common/
│       │           ├── fulcra_media/
│       │           ├── fulcra_attention/
│       │           ├── fulcra_dayone/
│       │           ├── fulcra_csv/
│       │           ├── rumps/
│       │           ├── pyobjc-*/
│       │           ├── keyring/
│       │           └── httpx/, fastapi/, uvicorn/, etc.
│       ├── plists/
│       │   └── com.fulcra.collect.plist    ← daemon launchd plist template
│       └── scripts/
│           └── fulcra-collect              ← daemon binary entrypoint (or just `python -m fulcra_collect.cli`)
```

The menubar app, when launched, runs the daemon as a subprocess (via SMAppService → launchd reads the plist → launches `python -m fulcra_collect.cli daemon`).

## The work, broken into phases

### Phase 1 — py2app config (the easy part)

Create `packages/menubar/setup.py` (or a `pyproject.toml` `[tool.py2app]` section if py2app supports it; it doesn't in the standard way, so probably setup.py). Configure:

- `APP = ['fulcra_menubar/__main__.py']`
- `OPTIONS = {'argv_emulation': False, 'plist': {...}, 'includes': [list of every fulcra-* package + all their deps], 'packages': [...]}`
- `LSUIElement: True` in Info.plist so it doesn't show in the Dock
- `LSMinimumSystemVersion: 13.0` (for SMAppService.agent(plistName:))
- `NSHumanReadableCopyright`, bundle id, etc.

Build with `python setup.py py2app`. Output: `dist/Fulcra Collect.app`.

**Likely surprises:**
- `pyobjc` brings in dozens of frameworks; py2app's auto-detection is usually fine but may miss some. Look at the run-time `from AppKit import ...` and `from ServiceManagement import ...` calls and explicitly include via `includes`.
- `uvicorn` has dynamic-import patterns (uvicorn workers, h11) — py2app misses these by default. Need `--includes uvicorn,uvicorn.protocols.http.h11_impl,...`.
- `keyring` has multiple backends; only the macOS one is needed but py2app may try to ship them all. Explicitly include `keyring.backends.macOS`.
- `fastapi` and `pydantic` use plenty of dynamic imports — may need `pyproject.toml` extras or explicit `includes` lists.

### Phase 2 — Daemon binary inside the bundle

Two design choices:

**Option A — single binary, daemon-as-subprocess:**
The menubar app's entry point launches the daemon as a Python subprocess from inside the bundle. Pros: only one binary to sign + notarize. Cons: SMAppService still needs a separate `plist` pointing at a real executable path, which complicates the "launchd starts the daemon at login" story.

**Option B — separate daemon binary:**
py2app produces `Fulcra Collect.app` AND `fulcra-collect-daemon` (a stripped-down entry that just runs `fulcra_collect.cli:cli(['daemon'])`). Plist points at the second binary. Pros: clean separation, matches today's CLI shape. Cons: two binaries to sign.

Lean toward **Option B** — closer to today's mental model, easier to reason about lifecycle.

### Phase 3 — Code-signing + notarization

Apple Gatekeeper blocks unsigned downloads from being opened with a `right-click → Open` dance. For a real product, need:

1. **Developer ID Application certificate** from Apple Developer Program ($99/yr) — `Developer ID Application: <Name> (<TEAM_ID>)`
2. **Hardened runtime** enabled (`codesign --options runtime`)
3. **Entitlements file** — at minimum `com.apple.security.cs.allow-jit` (for Python bytecode), maybe others depending on what we use
4. **Notarization** via `notarytool` — submit to Apple, wait ~5-15 min for them to scan, staple the ticket
5. **DMG signing** too (cosmetic — the .app inside is what matters but signed DMGs avoid an extra dialog)

This is where most .dmg packaging attempts die. Plan to spend a half-day getting through the first successful notarization round-trip.

**Scripted via:**
```bash
codesign --deep --force --options runtime \
  --entitlements packages/menubar/build/entitlements.plist \
  --sign "Developer ID Application: ..." \
  dist/Fulcra\ Collect.app

xcrun notarytool submit dist/Fulcra\ Collect.app.zip \
  --apple-id ... --team-id ... --password ... --wait

xcrun stapler staple dist/Fulcra\ Collect.app
```

### Phase 4 — DMG creation

`create-dmg` (homebrew formula) or `hdiutil` directly:

```bash
create-dmg \
  --volname "Fulcra Collect" \
  --window-pos 200 120 --window-size 600 400 \
  --icon-size 100 \
  --icon "Fulcra Collect.app" 175 190 \
  --hide-extension "Fulcra Collect.app" \
  --app-drop-link 425 190 \
  dist/Fulcra-Collect-${VERSION}.dmg \
  dist/Fulcra\ Collect.app
```

Result: a draggy-DMG with the app on the left and an Applications symlink on the right.

### Phase 5 — First-launch flow

When the user opens `Fulcra Collect.app` for the first time:

1. Menubar icon appears
2. SMAppService isn't registered yet → daemon bar shows "Install" button
3. Clicking Install: write the plist, register via SMAppService → triggers the one-time macOS "Login Items approval" dialog
4. User approves → daemon launches → menubar polling picks it up → status flips to "Running"
5. User clicks the popover → sees the same web-UI link they'd use today → onboarding starts in the browser

The first-launch flow is mostly already there from Sprint A's daemon-bar UI — Sprint C just makes it fire on a fresh install instead of after a manual `fulcra-collect install`.

### Phase 6 — Auto-update (deferred to Sprint D)

For a real product we'd want Sparkle (the macOS app updater framework) or equivalent. Out of scope for the first .dmg release — the user can re-download to update.

## Validation

Test the .dmg by:

1. Spinning up a clean macOS VM (or a clean user account)
2. Downloading the .dmg
3. Drag-installing
4. First launch from /Applications (NOT from `~/Downloads` — Gatekeeper treats them differently)
5. Walking the onboarding flow to first ingest

The clean-machine test is the only way to catch packaging gaps the dev machine masks.

## Time estimate

- Phase 1 + 2: 1 dedicated session (py2app surprises)
- Phase 3: half a day (notarization round-trips)
- Phase 4: 1 hour (create-dmg is well-trodden)
- Phase 5: minimal — Sprint A already did the UI work
- Validation: a couple hours of clean-machine iteration

Total: **2-3 focused sessions**. Code-signing + notarization eats most of the surprise budget.

## Risks

1. **py2app + uvicorn + fastapi:** they have a lot of dynamic-import patterns that py2app misses. Expect 2-3 rebuild-test cycles to find them all.
2. **Apple Developer Program signup time:** if the user doesn't have a Developer ID cert yet, this blocks Phase 3 entirely. Confirm cert availability before starting.
3. **Notarization rejections:** Apple sometimes flags hardened-runtime issues (missing entitlements, unsigned framework inside the bundle, etc.). Each iteration is ~10 min wait.
4. **Daemon HTTP port conflicts:** the .app launches the daemon on port 9292 by default. If the user's existing-install daemon is also running (from today's `uv run` path), they'll collide. The menubar should detect this and either prompt to stop the old one or pick a different port.
5. **State migration:** users running today's `uv tool install` version will have `~/.config/fulcra-collect/` state. The .app-installed daemon needs to keep using the same state directory (it should, since the keychain + config paths are user-level not bundle-relative). Verify.

## Dependency on Fulcra-side work

The user mentioned that Location / health / calendars / Strava need Fulcra-side upgrades before we can ship plugins for those data types. Those wait. The .dmg story doesn't depend on Fulcra-side anything — it's purely a Mac packaging problem.

## Recommendation

Don't start Sprint C tonight. The notarization round-trip will eat all the energy. Schedule a dedicated session with the Developer ID cert in hand.

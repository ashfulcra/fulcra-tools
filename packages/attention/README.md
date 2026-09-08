# fulcra-attention

Capture foreground browsing, with page titles and time-on-page, into your own
[Fulcra](https://fulcradynamics.com) account. The useful question is fairly
ordinary: *what was that article I read on Tuesday?* Capture depends on browser
permissions, activity detection, and your privacy settings; this is not a record
of every page you've ever opened. It belongs to the [unsupported monorepo](../../README.md).

The capture pipeline is **fully relayless**: the Chrome extension signs in through your browser with an Auth0 device flow and POSTs records **directly to the Fulcra API** (`https://api.fulcradynamics.com/ingest/v1/record/batch`). There is no localhost daemon involvement, no pairing, no per-extension token, and no relay route. The Python package in this repo is now just the Fulcra Collect *pointer* plugin — a static signpost that tells the user to install the browser extension and sign in.

## Install with the Collect download

The [Collect disk image](../../docs/collect.md#get-started-new-user) includes an
optional Chrome Attention extension folder. Follow its included instructions to
load it into Chrome, then sign in from the extension. Collect's Attention entry
points to that setup; installing the Mac app alone does not start browser capture.
The extension works independently of the Collect daemon.

For a source build, see the [Chrome README](chrome/README.md).

## Package contents

This package holds:

- **`fulcra_attention/`** — the Fulcra Collect pointer plugin (`collect_plugin.py`). It does no collection: it exists only so Collect still surfaces an "Attention" entry whose `run()` emits one informational message directing the user to build/load the extension and sign in via the browser. No credentials, no setup steps, no definition binding.
- **`chrome/`** — Chrome MV3 extension. Foreground-only capture, optional sharper-AFK content script, onboarding wizard, right-click context menu, branded UI. This is where all the real work happens — sign-in, definition resolution, and direct-to-Fulcra ingest. See [chrome/README.md](chrome/README.md) for build + load instructions.
- **[`safari/`](safari/)** — macOS and iOS containing apps and Safari extensions,
  sharing the browser capture code with native authentication and device identity.

## Setup

Setup happens entirely in the browser extension — there is nothing to configure in Fulcra Collect.

1. Build the extension: `npm ci && npm run build` in [`chrome/`](chrome/) (the unpacked output lands in `chrome/dist/`).
2. Load `chrome/dist/` as an unpacked extension (`chrome://extensions` → Developer mode → Load unpacked).
3. Open the extension and click **Connect to Fulcra**. Approve the browser sign-in page (Auth0 device flow); you're returned to the wizard.
4. Choose the **destination** — the Fulcra "Attention" annotation definition to save into, or create a fresh one — and **name this browser** (its per-browser identity label). Finish the wizard.

From then on the extension captures and ingests on its own, straight to the Fulcra API.

## Architecture

- **Relayless, direct-to-cloud.** The extension POSTs batches to `https://api.fulcradynamics.com/ingest/v1/record/batch` with a Bearer token obtained from its own Auth0 device-flow sign-in. No daemon, no loopback endpoint, no pairing handshake. See `chrome/src/relayless/` (`oidc.ts`, `signIn.ts`, `relaylessSender.ts`, `ensureDefinition.ts`, `wire.ts`, `config.ts`).
- **Per-device identity.** Every install carries an identity slug, and it is obtained two different ways depending on the browser. **Chrome** asks: onboarding will not continue until you name the browser, and that label both slugifies into a `machine:<slug>` tag and folds into the source_id. It is prefilled from the signed-in email (`<email> browser`) and editable in the wizard / popup. **Safari** does not ask: the containing app mints an automatic per-installation identity on first use and stores it in the shared App Group, because Safari has no onboarding wizard and iOS will not hand an app the user's device name without a specially-granted entitlement. Naming a Safari device is therefore *optional and additive* — the distinctness below holds with or without a name; a name only adds the readable `machine:<slug>` tag.
- **Each accepted event** becomes one `DurationAnnotation` under the resolved `Attention` definition, tagged `attention` + `web` (plus the `machine:<slug>` tag when a human label is set — so Chrome records carry it, and Safari records carry it only once you name that device).
- **Source-id namespace.** `com.fulcra.attention.v3.<sha256(scrubbed_key|start_time_second|identitySlug)[:16]>`. Different identity slugs make the same URL and second produce distinct source IDs; Chrome labels must therefore resolve to distinct slugs. Dedup is server-side on source_id; the extension also keeps a client-side sent-set to avoid re-POSTing.

  > **Identity needs care.** Safari's automatic identity lives in the App Group
  > container; deleting that data and reinstalling creates a new identity. A name
  > makes the device readable to a human but does not restore the old source IDs.
  > Chrome derives its slug from your chosen label, lowercases it, replaces
  > separators, and truncates it. Use distinct labels that differ near the start:
  > two labels that collapse to the same slug can collide on the same URL and second.

Three-tier privacy posture (Tier 1 always-on, Tiers 2 + 3 user-driven from the extension popup):

| Tier | Action | Default |
|---|---|---|
| **1 — Param strip** | Remove ~80 auth/tracking params | Always on |
| **2 — Categorize** | Replace URL/title with category slug (e.g. `banking`) | Empty by default |
| **3 — Ignore** | Drop event entirely | Empty by default |

## Multi-machine + multi-identity

Each browser signs in independently and carries an identity slug. Distinct slugs keep records from different installs distinguishable; use distinct Chrome labels as described above. Chrome additionally carries a `machine:<slug>` tag, which is what makes them distinguishable *by eye* at query time; a Safari device gets that tag once you name it, and until then it is distinct in the data but shows up unnamed. The extension's user-managed ignore list propagates across Chrome profiles via Chrome sync (`chrome.storage.sync`).

Users with multiple Chrome profiles (one per company / client / personal) have their `chrome_identity` carried through to `external_ids` on every annotation, so you can group by `external_ids.chrome_identity` at query time. The identity is captured from `chrome.identity.getProfileUserInfo()` (the Google account email signed into that Chrome profile) or a free-text user-set label.

## Development

```bash
# From the repository root after workspace setup:
uv run --package fulcra-attention --extra dev pytest packages/attention/tests/ -q
```

The browser extension is built and tested under [`chrome/`](chrome/) — see [chrome/README.md](chrome/README.md).

### Safari (macOS + iOS)

The Safari app and extension live under [`safari/`](safari/). Four Xcode targets
(app + extension, each for macOS and iOS) are built on every change by
[`.github/workflows/xcode.yml`](../../.github/workflows/xcode.yml), which also
runs the Swift test suite:

```bash
# Build BOTH extension bundles first. The test scheme builds the macOS app,
# which embeds the extension, which copies a built bundle as resources — so
# from a clean checkout the test fails on missing files that have nothing to
# do with the tests themselves.
cd packages/attention/chrome
npm ci && npm run build && npx vite build --config vite.safari.config.ts

cd ../safari/FulcraAttention
xcodebuild -project FulcraAttention.xcodeproj \
  -scheme FulcraAttentionTests -destination 'platform=macOS' test
```

`FulcraAttentionTests` is hosted by the macOS app, so `@testable import
FulcraAttention` reaches the app module. Platform-agnostic logic therefore
belongs in the **macOS app target** even when only the extension uses it at
runtime — that is what makes it testable.

**Shipping to TestFlight:** `safari/scripts/release_testflight.sh` does the whole
mechanical path — builds both JS bundles, archives, verifies the archive really
contains the embedded extension *with* its bundle inside, exports for App Store
Connect, validates, and uploads. Run it with `--dry-run` to exercise everything
except the upload.

The script checks these release prerequisites before building:

| Needed | What the release runner needs |
|---|---|
| **Apple Distribution** certificate | Available with its private key to the release runner. Developer ID Application is for direct distribution; it cannot sign an App Store build. |
| App record for `com.fulcra.attention` | Created once by hand in App Store Connect. |
| App Store Connect API key (`.p8`) | Downloadable exactly once, by the account holder. Pass via `ASC_KEY_ID` / `ASC_ISSUER_ID`. |

The build number (`CURRENT_PROJECT_VERSION`) must increase on every upload —
App Store Connect rejects a repeat.

## Status

- **Relayless extension:** shipped, lives under [chrome/](chrome/). Direct-to-Fulcra ingest via Auth0 device flow. Foreground-only attention, AFK detection, pause control, onboarding wizard, right-click context menu, branded UI.
- **Python package:** reduced to the Fulcra Collect pointer plugin (`collect_plugin.py`). The relay-era backend (CLI, `ingest.py`, `fulcra.py`, `state.py`) has been retired.

## License

Personal-use experiment; no package license is declared in [pyproject.toml](pyproject.toml).

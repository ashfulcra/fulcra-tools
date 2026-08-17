# fulcra-attention

Capture what takes your attention while browsing — every page you read, with title and time-on-page — into your own [Fulcra](https://fulcradynamics.com) account, so you can later recall *"what was that article I read on Tuesday?"*

The capture pipeline is **fully relayless**: the Chrome extension signs in through your browser with an Auth0 device flow and POSTs records **directly to the Fulcra API** (`https://api.fulcradynamics.com/ingest/v1/record/batch`). There is no localhost daemon involvement, no pairing, no per-extension token, and no relay route. The Python package in this repo is now just the Fulcra Collect *pointer* plugin — a static signpost that tells the user to install the browser extension and sign in.

This package holds:

- **`fulcra_attention/`** — the Fulcra Collect pointer plugin (`collect_plugin.py`). It does no collection: it exists only so Collect still surfaces an "Attention" entry whose `run()` emits one informational message directing the user to build/load the extension and sign in via the browser. No credentials, no setup steps, no definition binding.
- **`chrome/`** — Chrome MV3 extension. Foreground-only capture, optional sharper-AFK content script, onboarding wizard, right-click context menu, branded UI. This is where all the real work happens — sign-in, definition resolution, and direct-to-Fulcra ingest. See [chrome/README.md](chrome/README.md) for build + load instructions.

## Setup

Setup happens entirely in the browser extension — there is nothing to configure in Fulcra Collect.

1. Build the extension: `npm run build` in [`chrome/`](chrome/) (the unpacked output lands in `chrome/dist/`).
2. Load `chrome/dist/` as an unpacked extension (`chrome://extensions` → Developer mode → Load unpacked).
3. Open the extension and click **Connect to Fulcra**. Approve the browser sign-in page (Auth0 device flow); you're returned to the wizard.
4. Choose the **destination** — the Fulcra "Attention" annotation definition to save into, or create a fresh one — and **name this browser** (its per-browser identity label). Finish the wizard.

From then on the extension captures and ingests on its own, straight to the Fulcra API.

## Architecture

- **Relayless, direct-to-cloud.** The extension POSTs batches to `https://api.fulcradynamics.com/ingest/v1/record/batch` with a Bearer token obtained from its own Auth0 device-flow sign-in. No daemon, no loopback endpoint, no pairing handshake. See `chrome/src/relayless/` (`oidc.ts`, `signIn.ts`, `relaylessSender.ts`, `ensureDefinition.ts`, `wire.ts`, `config.ts`).
- **Per-device identity.** Every install carries an identity slug, and it is obtained two different ways depending on the browser. **Chrome** asks: onboarding will not continue until you name the browser, and that label both slugifies into a `machine:<slug>` tag and folds into the source_id. It is prefilled from the signed-in email (`<email> browser`) and editable in the wizard / popup. **Safari** does not ask: the containing app mints an automatic per-installation identity on first use and stores it in the shared App Group, because Safari has no onboarding wizard and iOS will not hand an app the user's device name without a specially-granted entitlement. Naming a Safari device is therefore *optional and additive* — the distinctness below holds with or without a name; a name only adds the readable `machine:<slug>` tag.
- **Each accepted event** becomes one `DurationAnnotation` under the resolved `Attention` definition, tagged `attention` + `web` (plus the `machine:<slug>` tag when a human label is set — so Chrome records carry it, and Safari records carry it only once you name that device).
- **Source-id namespace.** `com.fulcra.attention.v3.<sha256(scrubbed_key|start_time_second|identitySlug)[:16]>`. Folding the identity slug into the hash makes the same url+second from two different installs produce **distinct** source_ids (the multi-browser distinctness guarantee). Dedup is server-side on source_id; the extension also keeps a client-side sent-set to avoid re-POSTing.

  > **Caveat — a device can be counted twice, but two devices are never merged.** Safari's automatic identity lives in the app's App Group container, so deleting the app (or its data) and reinstalling mints a new one, and your history will show that device as two. It is deliberately built to fail in that direction: an over-count is visible and can be stitched back together at query time, whereas two devices sharing one identity would make server-side dedup silently discard one of them, and source_ids cannot be recomputed after the fact. If you want a device to stay recognisable across reinstalls, give it a name.

Three-tier privacy posture (Tier 1 always-on, Tiers 2 + 3 user-driven from the extension popup):

| Tier | Action | Default |
|---|---|---|
| **1 — Param strip** | Remove ~80 auth/tracking params | Always on |
| **2 — Categorize** | Replace URL/title with category slug (e.g. `banking`) | Empty by default |
| **3 — Ignore** | Drop event entirely | Empty by default |

## Multi-machine + multi-identity

Each browser signs in independently and carries its own identity slug, so records from different installs stay distinguishable. Chrome additionally carries a `machine:<slug>` tag, which is what makes them distinguishable *by eye* at query time; a Safari device gets that tag once you name it, and until then it is distinct in the data but shows up unnamed. The extension's user-managed ignore list propagates across Chrome profiles via Chrome sync (`chrome.storage.sync`).

Users with multiple Chrome profiles (one per company / client / personal) have their `chrome_identity` carried through to `external_ids` on every annotation, so you can group by `external_ids.chrome_identity` at query time. The identity is captured from `chrome.identity.getProfileUserInfo()` (the Google account email signed into that Chrome profile) or a free-text user-set label.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

The browser extension is built and tested under [`chrome/`](chrome/) — see [chrome/README.md](chrome/README.md).

### Safari (macOS + iOS)

The Safari app and extension live under [`safari/`](safari/). Four Xcode targets
(app + extension, each for macOS and iOS) are built on every change by
[`.github/workflows/xcode.yml`](../../.github/workflows/xcode.yml). Swift suites
run via `safari/scripts/run_swift_tests.sh` (a throwaway SwiftPM package — the
xcodeproj still has no test target).

**Shipping to TestFlight:** `safari/scripts/release_testflight.sh` does the whole
mechanical path — builds both JS bundles, archives, verifies the archive really
contains the embedded extension *with* its bundle inside, exports for App Store
Connect, validates, and uploads. Run it with `--dry-run` to exercise everything
except the upload.

It cannot run unattended yet, and it fails in preflight naming exactly what is
missing rather than part-way through:

| Needed | Why it is not automatable here |
|---|---|
| **Apple Distribution** certificate | Requires the Apple ID. This Mac has only *Apple Development* and *Developer ID Application* — and Developer ID signs a notarised `.dmg` for direct download, **not** an App Store build. Different certificate, common confusion. |
| App record for `com.fulcra.attention` | Created once by hand in App Store Connect. |
| App Store Connect API key (`.p8`) | Downloadable exactly once, by the account holder. Pass via `ASC_KEY_ID` / `ASC_ISSUER_ID`. |

The build number (`CURRENT_PROJECT_VERSION`) must increase on every upload —
App Store Connect rejects a repeat.

## Status

- **Relayless extension:** shipped, lives under [chrome/](chrome/). Direct-to-Fulcra ingest via Auth0 device flow. Foreground-only attention, AFK detection, pause control, onboarding wizard, right-click context menu, branded UI.
- **Python package:** reduced to the Fulcra Collect pointer plugin (`collect_plugin.py`). The relay-era backend (CLI, `ingest.py`, `fulcra.py`, `state.py`) has been retired.

## License

Personal-use project. No license declared yet.

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
- **Per-browser identity.** Each browser is named with an identity label that slugifies into a `machine:<slug>` tag appended to its records, so events from different browsers stay distinguishable. The label is prefilled from the signed-in email (`<email> browser`) and editable in the wizard / popup.
- **Each accepted event** becomes one `DurationAnnotation` under the resolved `Attention` definition, tagged `attention` + `web` (plus the `machine:<slug>` tag when a label is set).
- **Source-id namespace.** `com.fulcra.attention.v3.<sha256(scrubbed_key|start_time_second|identitySlug)[:16]>`. Folding the per-browser identity slug into the hash makes the same url+second from two different browsers produce **distinct** source_ids (the multi-browser distinctness guarantee). Dedup is server-side on source_id; the extension also keeps a client-side sent-set to avoid re-POSTing.

Three-tier privacy posture (Tier 1 always-on, Tiers 2 + 3 user-driven from the extension popup):

| Tier | Action | Default |
|---|---|---|
| **1 — Param strip** | Remove ~80 auth/tracking params | Always on |
| **2 — Categorize** | Replace URL/title with category slug (e.g. `banking`) | Empty by default |
| **3 — Ignore** | Drop event entirely | Empty by default |

## Multi-machine + multi-identity

Each browser signs in independently and carries its own `machine:<slug>` tag, so records from different browsers stay distinguishable at query time. The extension's user-managed ignore list propagates across Chrome profiles via Chrome sync (`chrome.storage.sync`).

Users with multiple Chrome profiles (one per company / client / personal) have their `chrome_identity` carried through to `external_ids` on every annotation, so you can group by `external_ids.chrome_identity` at query time. The identity is captured from `chrome.identity.getProfileUserInfo()` (the Google account email signed into that Chrome profile) or a free-text user-set label.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

The browser extension is built and tested under [`chrome/`](chrome/) — see [chrome/README.md](chrome/README.md).

## Status

- **Relayless extension:** shipped, lives under [chrome/](chrome/). Direct-to-Fulcra ingest via Auth0 device flow. Foreground-only attention, AFK detection, pause control, onboarding wizard, right-click context menu, branded UI.
- **Python package:** reduced to the Fulcra Collect pointer plugin (`collect_plugin.py`). The relay-era backend (CLI, `ingest.py`, `fulcra.py`, `state.py`) has been retired.

## License

Personal-use project. No license declared yet.

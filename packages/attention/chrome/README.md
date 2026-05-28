# fulcra-attention — Chrome MV3 extension

The browser-side half of [fulcra-attention](../README.md). Captures every page you visit (URL + title + OG description + favicon + time-on-page) and POSTs it to the fulcra-collect daemon at `http://127.0.0.1:9292/api/extension/attention`, which forwards it into your Fulcra account.

The daemon's port (default `9292`) is configurable via `[daemon] web_port` in `~/.config/fulcra-collect/config.toml`. The extension reads it from `chrome.storage.local` after the one-click pair flow.

## Install — prebuilt (no Node toolchain)

The easy path. You need **neither Node nor this repo** — just the zip.

1. Open the [**Releases** page](https://github.com/ashfulcra/fulcra-tools/releases) and download `fulcra-attention-chrome.zip` from the latest `attention-v*` release.
2. Unzip it — you get a `fulcra-attention-chrome/` folder.
3. Open `chrome://extensions/` and turn on **Developer mode** (top-right toggle).
4. Click **Load unpacked** and select the unzipped **`fulcra-attention-chrome/`** folder.
5. Open the fulcra-collect daemon's Preferences → Plugins → Attention and click **Pair extension**. The wizard hands the extension its bearer token and the daemon's port in one click.

Chrome keeps sideloaded extensions across restarts but shows a "Developer mode extensions" notice each launch — expected. One-click install + auto-update comes with the Chrome Web Store listing (v2, gated on the Auth0 work).

## Build from source (developers)

```bash
cd chrome
npm install         # or: pnpm install
npm run dev         # Vite dev mode with hot reload
npm test            # Vitest run (cross-language scrub gate included)
npm run build       # Production build to chrome/dist/
```

To load a from-source build: **Load unpacked** → select **`chrome/dist/`** (the build output), *not* the `chrome/` source folder.

> **Load `chrome/dist/`, not `chrome/`.** The source folder has no `manifest.json`,
> so Chrome rejects it with "Manifest file is missing or unreadable" — that error
> means you picked the wrong folder. Run `npm run build` and choose `chrome/dist/`.
> (`dist/` is gitignored, so it won't exist until you build.)

## Cutting a release

Bump `version` in `packages/attention/chrome/package.json`, commit, then push a matching tag:

```bash
git tag attention-v0.1.0
git push origin attention-v0.1.0
```

The `chrome-release` GitHub Actions workflow (at the monorepo root, `.github/workflows/`) builds the extension, runs the tests, and attaches `fulcra-attention-chrome.zip` to the auto-created GitHub Release. Run the workflow manually (`workflow_dispatch`) to produce the zip as a downloadable artifact without cutting a release.

## Architecture

- `src/background.ts` — MV3 service worker. **Foreground-only attention model**: a visit starts when a tab becomes the foreground tab (active in focused window) and is HTTP(S)-not-ignored. The visit accumulates focused time only while the user is not idle (chrome.idle threshold 30 s) and, if enabled, while the optional heartbeat content script reports recent input. Blur (window blur, tab activation switch, idle) pauses the visit for a 30 s grace window; returning resumes it, expiring emits it. Background tabs that never get focus produce zero events.
- `public/heartbeat.js` — **Optional content script** (off by default). When the user opts in, it runs on every page and posts `{kind:"heartbeat", t}` on any input event, debounced to 5 s. Reads no page content, only event types. Powers the sharper AFK signal. Requires the `<all_urls>` optional host permission, requested at runtime.
- `src/heartbeat-control.ts` — Wires `chrome.permissions.request` + `chrome.scripting.registerContentScripts` to the heartbeat toggle in the wizard and popup.
- `src/scrub.ts` — Tier 1 always-on URL scrubber. Byte-identical to the Python sibling via the shared fixture `../tests/fixtures/scrub_cases.json`. 66 cross-language contract cases.
- `src/categorize.ts` / `src/ignore.ts` — User-driven Tier 2 (categorize) and Tier 3 (ignore). Both default to empty; right-click "Fulcra Attention → Ignore this domain" / "Categorize as …" populate them inline.
- `src/identity.ts` — chrome_identity capture (Google account email or popup label override). Supports N>2 contexts (per-company Google accounts, free-text fallback).
- `src/content.ts` — Page-meta extractor (title, og:description, og:type, favicon, html lang). Injected on demand at visit close, not persistent.
- `src/outbox.ts` — Write-ahead queue in `chrome.storage.local`. POSTs to `http://127.0.0.1:9292/api/extension/attention`. Retries on alarm ticks every minute. Writes `lastIngestError` on 401 / repeated failures so the popup can surface a "Reconnect" / "Daemon unreachable" banner and the toolbar icon can swap to its error variant.
- `src/wizard/` — Onboarding flow tied to the daemon's pair handshake (welcome → pair → history scan → bulk-exclude → optional heartbeat consent → optional backfill → done with deeplink to fulcra.ai context dashboard).
- `src/popup/` — React popup: pause control (15 m / 30 m / 1 h / indefinite), pair status, today's counts, live last-5 stream, inline Tier 2 category editor, ignore list, heartbeat toggle, identity label.
- `src/options/` — Placeholder page that points back at the popup as the day-to-day surface.

## Storage map

The daemon's pair flow writes `bearerToken` and `relayPort` (the daemon's `web_port`) into `chrome.storage.local["settings"]`. There is no on-disk JSON config file on the extension side — everything lives in `chrome.storage.local` / `.sync` / `.session`.

| Where | What |
|---|---|
| `chrome.storage.local["settings"]` | bearer token, port, enabled, identity label, onboarded, pausedUntil, heartbeatEnabled |
| `chrome.storage.local["outbox"]` | pending POST queue |
| `chrome.storage.local["lastIngestError"]` | `{kind: "unauthorized" \| "unreachable", at}` or absent |
| `chrome.storage.local["categoryMap"]` | Tier 2 domain → category mappings |
| `chrome.storage.local["recentEmitted"]` | last 10 events for popup display |
| `chrome.storage.local["counts"]` | today's logged / categorized / ignored counters |
| `chrome.storage.sync["ignoreList"]` | Tier 3 — propagates across Chrome profiles |
| `chrome.storage.session["visits"]` | per-tab visit state (focused / blurred + focus time accumulator) |

## Manual smoke test

After install + pair:

1. Open a fresh tab → visit `https://example.com/`
2. Open another fresh tab → visit `https://news.ycombinator.com/`
3. Open the popup. The "Last 5 captured" stream should show one of those (depending on which closed first).
4. On the Plan A side: `fulcra get-records --type DurationAnnotation --start "5 minutes ago" | jq '.[] | select(.data.service == "web")'` and confirm the events landed in Fulcra.

## v2 roadmap

- OAuth (Auth0) direct from extension — drops the daemon dependency for remote users. See [`../docs/AUTH0_APPLICATION.md`](../docs/AUTH0_APPLICATION.md).
- Highlights (text selection → annotation linked to parent visit)
- Retrieval surface (popup search bar)
- Tier 2 editor in options page (v1.5)
- Safari Web Extension wrapper (separate project)

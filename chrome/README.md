# fulcra-attention — Chrome MV3 extension

The browser-side half of [fulcra-attention](../README.md). Captures every page you visit (URL + title + OG description + favicon + time-on-page) and POSTs it to the loopback relay at `127.0.0.1:8771`, which forwards it into your Fulcra account.

## Develop

```bash
cd chrome
npm install         # or: pnpm install
npm run dev         # Vite dev mode with hot reload
npm test            # Vitest run (cross-language scrub gate included)
npm run build       # Production build to dist/
```

## Load into Chrome

1. Build: `npm run build`
2. Open `chrome://extensions/`
3. Enable "Developer mode" (top right)
4. Click "Load unpacked"
5. Choose `chrome/dist/`
6. Open the extension popup and paste the bearer token from `~/.config/fulcra-attention/relay.json`

## Architecture

- `src/background.ts` — MV3 service worker. Subscribes to `webNavigation.onCommitted` + `onHistoryStateUpdated` (top frame only), runs a per-tab active-visit state machine, closes the visit on next nav / tab close / window blur, then queues an `AttentionEvent` in the outbox.
- `src/scrub.ts` — Tier 1 always-on URL scrubber. Byte-identical to the Python sibling via the shared fixture `../tests/fixtures/scrub_cases.json`. 55 contract tests must all pass.
- `src/categorize.ts` / `src/ignore.ts` — User-driven Tier 2 (categorize) and Tier 3 (ignore). Both default to empty.
- `src/identity.ts` — chrome_identity capture (Google account email or popup label override). Supports N>2 contexts (per-company Google accounts, free-text fallback).
- `src/content.ts` — Page-meta extractor (title, og:description, og:type, favicon, html lang).
- `src/outbox.ts` — Write-ahead queue in `chrome.storage.local`. Retries on alarm ticks every 60s.
- `src/popup/` — React popup: bearer token, on/off, daily counts, live last-5 stream, ignore list editor, identity label.
- `src/options/` — Placeholder for v1.5 Tier 2 editor.

## Storage map

| Where | What |
|---|---|
| `chrome.storage.local["settings"]` | bearer token, port, enabled toggle, identity label |
| `chrome.storage.local["outbox"]` | pending POST queue |
| `chrome.storage.local["categoryMap"]` | Tier 2 domain → category mappings |
| `chrome.storage.local["recentEmitted"]` | last 10 events for popup display |
| `chrome.storage.local["counts"]` | today's logged / categorized / ignored counters |
| `chrome.storage.sync["ignoreList"]` | Tier 3 — propagates across Chrome profiles |
| `chrome.storage.session["activeVisits"]` | open per-tab visit state |

## Manual smoke test

After install + paste bearer token:

1. Open a fresh tab → visit `https://example.com/`
2. Open another fresh tab → visit `https://news.ycombinator.com/`
3. Open the popup. The "Last 5 captured" stream should show one of those (depending on which closed first).
4. On the Plan A side: `fulcra get-records --type DurationAnnotation --start "5 minutes ago" | jq '.[] | select(.data.service == "web")'` and confirm the events landed in Fulcra.

## v2 roadmap

- OAuth (Auth0) direct from extension — drops the relay dependency. See `docs/superpowers/specs/2026-05-18-fulcra-browse-extension-auth0-app.md` in the sibling FulcraMediaHelpers repo.
- Highlights (text selection → annotation linked to parent visit)
- Retrieval surface (popup search bar)
- Tier 2 editor in options page (v1.5)
- Safari Web Extension wrapper (separate project)

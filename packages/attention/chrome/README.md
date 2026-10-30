# fulcra-attention — Chrome MV3 extension

The browser-side half of [fulcra-attention](../README.md). Captures foreground
browsing time and available page metadata, subject to permissions and your
privacy settings, and POSTs records directly to Fulcra. It works independently
of the Collect daemon. This is part of the [unsupported monorepo](../../../README.md).

The extension signs in through your browser with an Auth0 device flow and gets its own Bearer token. It then resolves an "Attention" annotation definition and ingests on its own. No `chrome.storage`-side port config — there is no daemon to point at.

## Install — prebuilt (no Node toolchain)

The [Collect disk image](../../../docs/collect.md#get-started-new-user) includes
an optional Chrome extension folder with loading instructions. A separately
published `fulcra-attention-chrome.zip` can be loaded the same way:

1. Open the [**Releases** page](https://github.com/ashfulcra/fulcra-tools/releases) and choose an `attention-v*` release that includes `fulcra-attention-chrome.zip`.
2. Unzip it — you get a `fulcra-attention-chrome/` folder.
3. Open `chrome://extensions/` and turn on **Developer mode** (top-right toggle).
4. Click **Load unpacked** and select the unzipped **`fulcra-attention-chrome/`** folder.
5. Open the extension and click **Connect to Fulcra**. Approve the browser sign-in page (Auth0 device flow); you're returned to the wizard. Choose the **destination** — the Fulcra "Attention" annotation definition to save into, or create a fresh one — and **name this browser** (its per-browser identity label). Finish the wizard and capture begins.

These are unpacked-extension instructions. They do not provide Chrome Web Store
installation or automatic updates. For another build, replace the folder and
reload the extension from `chrome://extensions/`.

## Build from source (developers)

From this directory (`packages/attention/chrome/`):

```bash
npm ci              # use the tracked lockfile; release CI uses Node 22
npm test            # Vitest run (cross-language scrub gate included)
npm run build       # Production build to packages/attention/chrome/dist/
npm run dev         # optional long-running Vite development server
```

To load a from-source build: **Load unpacked** → select the **`dist/`** build output (`packages/attention/chrome/dist/`), *not* the `packages/attention/chrome/` source folder. Then click **Connect to Fulcra** and complete the device-flow sign-in as in the install steps above.

> **Load `dist/`, not the source folder.** The source folder has no `manifest.json`,
> so Chrome rejects it with "Manifest file is missing or unreadable" — that error
> means you picked the wrong folder. Run `npm run build` and choose `packages/attention/chrome/dist/`.
> (`dist/` is gitignored, so it won't exist until you build.)

The [v1 development contract and journal](../README.md#v1-contract-development)
are opt-in foundations. Default capture/auth/destination/queue paths still use
the existing transport. [Journal tests](tests/relayless/v1Journal.test.ts) run
against the pinned development-only `fake-indexeddb`; the browser implementation
uses native IndexedDB and requires actual upgrade/persistence QA before rollout.

The opt-in [account-scoped HTTP helper](src/relayless/v1Http.ts) adds API principal
proof, current gate/token checks, captured finite JSON, bounded decoded-body
retention and no blind mutation retries. It performs no auth publication or
refresh, and default senders do not call it. The same linked rollout gates apply;
see its [mock-only tests](tests/relayless/v1Http.test.ts).

The opt-in [tag resolver](src/relayless/v1Tags.ts) adds account-scoped durable
creation and immutable UUID bindings. Its journal upgrades from IndexedDB v1
without clearing prior gates or datatype intents. Lost/unknown creation never
permits an automatic second POST, and a known UUID is retained before required
readback. See the [synthetic upgrade/restart tests](tests/relayless/v1Tags.test.ts)
and linked development contract; actual browser upgrade/persistence is still a
rollout gate.

The opt-in [owned-type inventory](src/relayless/v1OwnedTypes.ts) reads the direct
authenticated input endpoint with complete-response and whole-list validation.
It preserves retired types and returns frozen account-bound entries; it does
not bind destinations, create types or authorize uploads. Its
[HTTP-integrated synthetic tests](tests/relayless/v1OwnedTypes.test.ts) retain
the same actual-browser rollout limits.

The pure [Attention schema proof](src/relayless/v1AttentionSpec.ts) adds the
pinned Python-compatible creation specification and a closed served-schema
check. Unsupported constraints remain held. Its retained schema-text digest
has the exact definition in the linked development contract; it is not account
or upload authority. See the [synthetic tests](tests/relayless/v1AttentionSpec.test.ts).

The opt-in [destination setup binder](src/relayless/v1Destination.ts) combines
fresh ownership, input/served-schema proof and a current-generation journal
commit. It writes local bindings and only explicitly permitted remote type
creation; it is not a preview or record sender. Unknown creation is reconciled
without blind retry, known UUIDs remain immutable, and one journal handle must
serve each operation's transport and local effects. See the
[race/restart tests](tests/relayless/v1Destination.test.ts) and linked contract
for input-transform handling and remaining rollout gates.

The source-only worker now has an explicit null-payload `protection-setup`
command using the real DNR class under existing broker ownership. It returns
only installed-as-of-read; it cannot complete destination setup or activate
capture/drains. There is no automatic UI caller or boot installation. Native
calls that outlive cancellation retain the installer slot until settlement;
unknown effects are not rolled back. See the [source contract and native gates](../README.md#explicit-protection-setup-source).

The inert [candidate presentation adapter](src/relayless/v1DestinationCandidates.ts)
lets the Wizard display a strictly bounded future candidate-only response. It
adds no production listing, selection, completed setup or upload authority;
the actual worker still reports `SETUP_HELD`. See the canonical
[contract and remaining gates](../README.md#candidate-presentation-preparation).
[Test setup](tests/setup.ts) denies global fetch before imports and reinstalls the
guard before every case, so a previous `vi.unstubAllGlobals()` cannot leave the
next case unguarded. Later per-case fixture transports may override it. This is a
JavaScript fetch-seam guard, not a socket/OS/native-browser sandbox; a fixture
resetting globals within a case must install a synthetic transport before a
request caller. See the [safe consecutive-case regression](tests/fetch-guard.test.ts).
Synthetic UI messages and fake Chrome/IndexedDB are not installed-browser or
provider acceptance.

## Cutting a release

Bump the version in [package.json](package.json) and the Chrome/Safari manifest
configs, keeping the lockfile in sync, then commit and push a matching unused
`attention-v<version>` tag. The manifest is what the installed extension reads.

The [chrome-release workflow](../../../.github/workflows/chrome-release.yml)
builds the extension, runs the tests, and attaches `fulcra-attention-chrome.zip`
to the GitHub Release. A manual `workflow_dispatch` produces a downloadable
workflow artifact without publishing a release.

## Architecture

- `src/background.ts` — MV3 service worker. **Foreground-only attention model**: a visit starts when a tab becomes the foreground tab (active in focused window) and is HTTP(S)-not-ignored. The visit accumulates focused time only while the user is not idle (chrome.idle threshold 30 s) and, if enabled, while the optional heartbeat content script reports recent input. Blur (window blur, tab activation switch, idle) pauses the visit for a 30 s grace window; returning resumes it, expiring emits it. Background tabs that never get focus produce zero events.
- `public/heartbeat.js` — **Optional content script** (off by default). When the user opts in, it runs on every page and posts `{kind:"heartbeat", t}` on any input event, debounced to 5 s. Reads no page content, only event types. Powers the sharper AFK signal. Requires the `<all_urls>` optional host permission, requested at runtime.
- `src/heartbeat-control.ts` — Wires `chrome.permissions.request` + `chrome.scripting.registerContentScripts` to the heartbeat toggle in the wizard and popup.
- `src/scrub.ts` — Tier 1 always-on URL scrubber, checked against the
  [shared scrub fixtures](../tests/fixtures/scrub_cases.json) retained from the
  Python implementation.
- `src/categorize.ts` / `src/ignore.ts` — User-driven Tier 2 (categorize) and Tier 3 (ignore). Both default to empty; right-click "Fulcra Attention → Ignore this domain" / "Categorize as …" populate them inline.
- `src/identity.ts` — chrome_identity capture (Google account email or popup label override). Supports N>2 contexts (per-company Google accounts, free-text fallback).
- `src/content.ts` — Page-meta extractor (title, og:description, og:type, favicon, html lang). Injected on demand at visit close, not persistent.
- `src/outbox.ts` — Write-ahead queue in `chrome.storage.local`. POSTs batches **directly to the Fulcra API** (`https://api.fulcradynamics.com/ingest/v1/record/batch`) with the device-flow Bearer token, via `src/relayless/relaylessSender.ts`. Retries on alarm ticks every minute. Writes `lastIngestError` on 401 / repeated failures so the popup can surface a "Reconnect" / "Fulcra unreachable" banner and the toolbar icon can swap to its error variant.
- `src/relayless/` — The direct-to-Fulcra path: `oidc.ts` / `signIn.ts` (Auth0 device-flow sign-in), `relaylessSender.ts` (batch POST to the ingest endpoint), `ensureDefinition.ts` (resolve / create the "Attention" annotation definition), `wire.ts` (record wire-format), `config.ts` (API + auth config).
- `src/wizard/` — Onboarding flow built on the device-flow sign-in (welcome → **Connect to Fulcra** → choose destination definition → name this browser → history scan → bulk-exclude → optional heartbeat consent → optional backfill → done with deeplink to fulcra.ai context dashboard).
- `src/popup/` — React popup: pause control (15 m / 30 m / 1 h / indefinite), sign-in status, today's counts, live last-5 stream, inline Tier 2 category editor, ignore list, heartbeat toggle, identity label.
- `src/options/` — Placeholder page that points back at the popup as the day-to-day surface.

## Storage map

Authentication, destination resolution, and user settings have separate keys in
extension storage. Tokens and queued browsing records are sensitive; do not
paste a storage dump into an issue. There is no daemon-side configuration file.

| Where | What |
|---|---|
| `chrome.storage.local["settings"]` | enabled, identity label, onboarded, pausedUntil, heartbeatEnabled |
| `chrome.storage.local["relaylessTokens"]` | access/refresh tokens and expiry metadata |
| `chrome.storage.local["relaylessResolvedAttention"]` | resolved definition ID and tag IDs |
| `chrome.storage.local["outbox"]` | pending POST queue |
| `chrome.storage.local["lastIngestError"]` | `{kind: "unauthorized" \| "unreachable", at}` or absent |
| `chrome.storage.local["categoryMap"]` | Tier 2 domain → category mappings |
| `chrome.storage.local["recentEmitted"]` | last 10 events for popup display |
| `chrome.storage.local["counts"]` | today's logged / categorized / ignored counters |
| `chrome.storage.sync["ignoreList"]` | Tier 3 — propagates across Chrome profiles |
| `chrome.storage.session["visits"]` | per-tab visit state (focused / blurred + focus time accumulator) |

Choose distinct browser labels: the source-ID slug is derived from the label,
normalized and truncated, so two labels can collide. See the
[identity limitations](../README.md#architecture) in the parent README. URL
parameter stripping reduces exposure; it does not make page titles, paths,
descriptions, or the Chrome profile identity non-private. Configure categorize
and ignore rules before capturing sensitive browsing.

## Manual smoke test

After install + **Connect to Fulcra** sign-in:

1. Open a fresh tab → visit `https://example.com/`
2. Open another fresh tab → visit `https://example.org/`
3. Open the popup. The "Last 5 captured" stream should show one of those (depending on which closed first).
4. Open your chosen Attention definition in Fulcra and confirm those events
   appear. A local popup entry alone does not prove cloud delivery.

## Roadmap

- Highlights (text selection → annotation linked to parent visit)
- Retrieval surface (popup search bar)
- Tier 2 editor in options page (v1.5)
- Safari's app and extension already live in [safari/](../safari/); see the
  [parent README](../README.md#safari-macos--ios) for build and release details.

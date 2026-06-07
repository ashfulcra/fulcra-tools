# Relayless attention + mobile Safari

**Status:** design (approved direction; pending spec review)
**Date:** 2026-06-04
**Author:** Claude (with Ash)

## Problem

The Fulcra Attention browser extension today posts events to a **localhost
daemon** (`fulcra-collect` at `127.0.0.1:9292/api/extension/attention`), which
authenticates the user, dedups, and forwards to the Fulcra cloud. That model:
- requires running a daemon (friction; impossible on mobile),
- can't work on iOS (no localhost daemon; the phone roams off the LAN).

We want **(1) a relayless attention extension** — no daemon, posting straight
to the Fulcra cloud — and **(2) a mobile Safari attention plugin**, which is
relayless by necessity.

## Key enabler (already shipped in the library)

The `fulcra_api` Python library (`fulcra_api/oidc.py`) implements the **Auth0
device flow** — which is **redirect-less**, so it needs neither the daemon nor
the `chromiumapp.org`-redirect Auth0 app that the earlier `AUTH0_APPLICATION.md`
contemplated. The extension can replicate it directly. Public config (from
`fulcra_api/core.py`, reusable as-is):

| Field | Value |
|---|---|
| OIDC domain | `fulcra.us.auth0.com` |
| client_id (public) | `48p3VbMnr5kMuJAUe9gJ9vjmdWLdnqZt` |
| audience | `https://api.fulcradynamics.com/` |
| scope | `openid profile name email offline_access` |
| device-code endpoint | `POST https://fulcra.us.auth0.com/oauth/device/code` |
| token endpoint | `POST https://fulcra.us.auth0.com/oauth/token` |
| ingest endpoint | `POST https://api.fulcradynamics.com/ingest/v1/record/batch` |

> **One Auth0 prerequisite to verify (not assume):** that the device grant
> (`urn:ietf:params:oauth:grant-type:device_code`) is enabled for client
> `48p3VbMnr5kMuJAUe9gJ9vjmdWLdnqZt` with the API audience. The CLI uses device
> flow against this client, so it almost certainly is — confirm before building.

## Goals

1. Relayless attention: device-flow sign-in + direct cloud ingest, no daemon.
2. Reuse a **platform-agnostic core** (auth + transport + dedup + payload) so
   desktop Chrome and iOS Safari share it.
3. Mobile Safari attention via a native-app-wrapped Safari Web Extension,
   distributed by **TestFlight**.

## Non-goals

- Removing the daemon transport entirely — keep `relay` mode for daemon users.
- App Store public release (TestFlight only for now).
- Backfill on iOS (no `chrome.history` there).
- A new Auth0 application (the existing public client + device flow suffice).

## Architecture

**One core, two shells, transport-as-a-mode.**

```
            ┌─────────────────── relayless core (platform-agnostic TS) ──────────────────┐
            │  auth: device flow (device/code → poll token → store+refresh)              │
            │  transport: POST ingest/v1/record/batch (Bearer)                           │
            │  dedup: local "sent source_ids" set (+ existing flush mutex)               │
            │  payload: the AttentionEvent wire shape (unchanged)                        │
            └───────────────────────────────────────────────────────────────────────────┘
                 ▲                                                  ▲
   Chrome shell (capture: tabs/idle/windows)        iOS Safari shell (capture: content-script visibility)
                 │                                                  │
          transport mode: relay | relayless                native app wrapper + TestFlight
```

**Transport is a mode, not a fork.** `relay` = post to the daemon (today's
behavior + the daemon's `forwarded_events` dedup). `relayless` = device flow +
direct cloud. New/daemonless installs default to **relayless**; users running
the daemon may keep **relay**. Mobile is relayless-only. The mode is one setting;
the capture code is identical per platform regardless of mode.

## Sub-project 1 — Relayless core + Chrome shell

### Auth (device flow, in TS)
Replicate `oidc.py`:
1. `POST /oauth/device/code` with `{client_id, audience, scope}` →
   `{device_code, verification_uri_complete, user_code, interval}`.
2. UI: show/open `verification_uri_complete` (it embeds the code) so the user
   approves in a tab.
3. Poll `POST /oauth/token` with `{client_id, grant_type: device_code,
   device_code}` at `interval` until it returns `{access_token, refresh_token,
   expires_in}`. Handle `authorization_pending` / `slow_down`.
4. Store tokens in `browser.storage.local`; refresh via `grant_type:
   refresh_token` before expiry (and on a 401).

This replaces the daemon's `extension-token` as the credential in `relayless`
mode. `relay` mode keeps the pasted/paired daemon token.

### Transport
Outbox (`outbox.ts`) gains a mode switch: `relay` → `EXTENSION_ENDPOINT_URL`
(daemon); `relayless` → `https://api.fulcradynamics.com/ingest/v1/record/batch`
with `Authorization: Bearer <access_token>`. Same batching/retry/backoff; the
401 path triggers a token refresh (relayless) instead of a "reconnect to
daemon" banner.

**Event→wire-record transform (the substantive part).** In `relay` mode the
daemon receives the simple `AttentionEvent` and builds the full Fulcra ingest
record — it computes the attention `source_id`
(`com.fulcra.attention.v2.<hash>` = sha256 of key|start_time-to-second), binds
the `attention`/`web` tags + the bound Attention `DurationAnnotation`
definition id, and shapes the `recorded_at`/`note`/`sources` wire payload
(`fulcra_attention.ingest.build_attention_event` + `fulcra_common.wire`). In
`relayless` mode there's no daemon, so the **extension must port this transform
to TS** and emit the full record itself. This is a core sub-component, kept in
the platform-agnostic core and unit-tested against the Python transform's
output (golden fixtures) so the wire shape matches byte-for-byte.

### Dedup (relayless)
The daemon's `forwarded_events` server claim isn't present in relayless mode.
Use a **client-side sent-set**: a bounded set of already-POSTed attention
`source_id`s in `browser.storage.local`, consulted before each POST and updated
on a successful (2xx) send — mirroring `forwarded_events` locally. Combined with
the **flush mutex** (already shipped) this prevents the intra-device re-POST
duplication that caused the 13-day storm. (Cross-*device* isn't a dup case for
attention — each device's browsing is genuinely distinct; the cross-source
content fingerprints still carry for any query-time merge.)

### Onboarding
The popup/wizard gains a relayless sign-in: "Sign in with Fulcra" → runs the
device flow → shows the code/URL → on success shows "signed in as
<email>" (`GET /user/v1alpha1/me`). The definition/tags the daemon used to
ensure (the `Attention`/`Browsed` def, `attention`/`web` tags) must now be
ensured by the extension on first run via the data API (the AUTH0 doc lists the
exact endpoints).

### Factoring
Extract `auth/` (device flow + token store), `transport/` (mode + ingest), and
`dedup/` (sent-set) into a platform-agnostic core module; keep `background.ts`
(Chrome capture) as the Chrome shell consuming the core.

## Sub-project 2 — Mobile Safari shell

### Build path
Run Apple's `xcrun safari-web-extension-converter <chrome-ext-dir>` on the
relayless extension → an Xcode project with a **native iOS app** + a **Safari
Web Extension** target. The web-extension JS reuses the core; the capture layer
is rewritten for iOS.

### Capture model (iOS limits)
No `chrome.idle`, no `chrome.windows` focus, no `chrome.history`, no persistent
background. So:
- A **content script** on each page records a visit: start on first
  `visibilitychange→visible` / `pageshow`, end on `visibilitychange→hidden` /
  `pagehide`, accumulating visible foreground time. It builds the same
  `AttentionEvent` and hands it to the background (or enqueues directly).
- The **background** flushes the outbox when woken (event-driven; iOS suspends
  it aggressively). Flush opportunistically on `pagehide` so tail visits aren't
  lost.
- No backfill (history API absent).

### Auth on iOS
The **same device flow** — open `verification_uri_complete` in a Safari tab,
poll `/oauth/token`. No native `ASWebAuthenticationSession` needed (device flow
is browser-only). Tokens in the extension's `browser.storage.local` (App Group
sharing with the native app only if the app needs to show auth state).

### Native app + distribution
The native app is a thin container (a simple "open Safari → enable the
extension → sign in" onboarding screen). Distribution: **TestFlight** — Apple
Developer account, App ID, App Store Connect record, signed build via Xcode,
internal testing. (App Store public release is a later, separate step.)

## Data flow (relayless)

capture (Chrome bg / iOS content script) → `AttentionEvent` → outbox
(`browser.storage.local`) → flush (mutex): for each event, skip if its
`source_id` ∈ sent-set; else `POST ingest/v1/record/batch` with the device-flow
Bearer (refresh on 401); on 2xx add `source_id` to the sent-set and drop the
entry.

## Testing

- **Core (vitest, platform-agnostic):** device-flow state machine (pending/
  slow_down/success/expiry), token refresh on 401, transport mode switch,
  sent-set dedup (skip already-sent; record on 2xx), payload shape. Mock fetch.
- **Chrome shell:** existing capture tests stay; add relayless-mode transport
  tests.
- **iOS capture:** unit-test the content-script visit state machine
  (visible/hidden/duration) against a jsdom/visibility fixture.
- **Device-only / TestFlight:** a manual smoke checklist (sign-in on device,
  visit pages, confirm events land in Fulcra) — not automatable here.

## Risks / blockers

- **iOS native + TestFlight are not automatable from this repo** — they need
  Ash's Mac, Xcode, and an Apple Developer account/App Store Connect. The JS +
  converter scaffolding is buildable; the signed app + TestFlight upload is a
  human step.
- **iOS background suspension** may still drop tail-end visits; mitigated by
  `pagehide` flush, but accept best-effort durations.
- **Auth0 device-grant enablement** for the public client must be verified (see
  prerequisite above).
- **Definition/tag ensuring** moves from the daemon into the extension in
  relayless mode — must be idempotent and not duplicate defs (reuse the
  cross-source/resolver dedup posture).
- The `safari-web-extension-converter` output typically needs manual surgery
  for the iOS capture differences (it converts APIs 1:1 but iOS lacks several).

## Sequencing

1. **Relayless core** (auth + transport mode + sent-set dedup), platform-
   agnostic, vitest-covered. Implementable now.
2. **Chrome relayless shell** — wire the core into the existing extension; add
   the device-flow onboarding; ensure def/tags on first run. Implementable now.
3. **iOS Safari shell** — convert, rewrite capture for iOS, build the native
   app. JS/scaffolding implementable now; the signed build + TestFlight is
   Ash's step.

Steps 1–2 ship "Chrome without a daemon" independently and de-risk everything
the iOS shell reuses.

---

## Addendum (2026-06-07): Safari/iOS — empirically confirmed blocker + refined native architecture

This addendum supersedes the original "Auth on iOS" section, based on a live
test plus a peer review on the coordination bus.

### Empirically confirmed: the extension cannot do the Auth0 sign-in on Safari

We converted the relayless Chrome extension with
`xcrun safari-web-extension-converter`, built the macOS app target, loaded it in
Safari, and clicked "Connect to Fulcra". Result: **`device code request failed:
HTTP 403`** — the exact failure Chrome had before the declarativeNetRequest
Origin-strip.

Root cause (Apple docs + this live test):
- The extension's `POST /oauth/device/code` carries the extension `Origin`;
  Auth0 rejects any non-allowlisted Origin ("Allowed Web Origins" check).
- On Chrome we strip that Origin via `declarativeNetRequest`. **Safari refuses:**
  `Origin`/`Host` are disallowed *sensitive* headers, and DNR `modifyHeaders`
  does not apply to *extension-initiated* requests. `chrome.identity` is also
  unsupported (so the redirect/`launchWebAuthFlow` path is out too).
- The converter additionally flags `idle` and `history` as unsupported → no idle
  detection, no backfill.

Conclusion: **the relayless extension's JS-side auth is not portable to Safari.**
Auth must move into the native app.

### Refined architecture — native owns auth + tokens [+ ingest]; JS only captures

Apple's Safari Web Extension model is native app + extension JS + native
extension in separate sandboxes, with **App Groups** for shared native data and
**nativeMessaging** for JS↔native. The Safari/iOS build is therefore a different
shape than Chrome:

- **Auth — native app.** `ASWebAuthenticationSession` + Auth0 **native PKCE**
  (cleaner UX than the device flow; or device-flow-via-`URLSession` to avoid
  registering a callback URI). Native HTTP carries no browser Origin → no 403.
- **Tokens — native, in the Keychain (access group).** Refresh + access tokens
  live in the Keychain, NOT in `browser.storage.local` or App Group
  `UserDefaults`. JS never holds the refresh token.
- **Ingest — native (preferred).** Native POSTs to the Fulcra ingest API via
  `URLSession`; JS forwards captured events to native and native builds the wire
  record + POSTs. (Alternative: native hands JS a *short-lived access token* and
  JS reuses `relayless/wire` + `relaylessSender` — only if avoiding a Swift
  wire-port is worth a short-lived token in JS.)
- **Wiring.** Content scripts can't talk to native directly:
  `content script → background/event-page → sendNativeMessage(native)`. On iOS,
  the extension JS requests/reads via the native extension, not containing-app
  push.
- **Background.** Safari prefers an **event page** over a service worker → add
  `background.scripts` / `preferred_environment`; event-page lifetime is
  undependable.
- **Capture.** The page-visibility capture
  (`attention/chrome/src/capture/visibility.ts`, shipped on this branch) detects
  visits and emits `AttentionEvent`s; flush **opportunistically** (on
  `pagehide`) because the event page can be killed at any time. No backfill.

### Native-layer implementation spec (the remaining Swift work)

In `attention/safari/` (converter scaffold, then hand-built):
1. **Auth (Swift):** `ASWebAuthenticationSession` PKCE against the Auth0 public
   client `48p3VbMnr5kMuJAUe9gJ9vjmdWLdnqZt` (audience
   `https://api.fulcradynamics.com/`). Store refresh+access tokens in a Keychain
   access group shared by app + native extension; refresh on 401/expiry.
2. **Ingest (Swift):** a `URLSession` poster to
   `POST https://api.fulcradynamics.com/ingest/v1/record/batch` with the Keychain
   access token; build the same wire record as `relayless/wire.ts` (port
   `source_id`, the data-inner shape, the def/tag binding incl. the
   `machine:<slug>` identity tag — port `ensureDefinition`).
3. **Native messaging bridge (Swift `NSExtensionRequestHandling`):** receives
   `AttentionEvent` batches from the extension JS (`sendNativeMessage`), hands
   them to the ingest poster; returns auth state (signed-in? identity label?) to
   JS for the popup.
4. **Extension JS (reuse):** visibility capture (this branch) →
   background/event-page batches → `sendNativeMessage`. The popup shows auth
   state from native (no token in JS).
5. **Onboarding:** the native app screen drives sign-in (PKCE) + "name this
   browser"; the extension popup shows status.

Distribution: **TestFlight** — Apple Developer account, App ID + App Group +
Keychain-access-group entitlements, App Store Connect record, signed build.
(Needs Ash's Apple Developer account; not automatable from this repo.)

### Sequencing (Safari/iOS)
1. ✅ Visibility capture content-script (this branch) — platform-agnostic, tested.
2. Convert + commit the Xcode scaffold (`attention/safari/`); add the event-page
   `background.scripts` config; wire the visibility capture as the iOS content
   script.
3. Native auth (PKCE → Keychain) + the nativeMessaging bridge.
4. Native ingest (`URLSession` + wire port), or short-lived-token-to-JS.
5. TestFlight signed build (Ash's Apple account).

# Relayless attention + mobile Safari

**Status:** design — Chrome track approved/shipping; Safari/iOS track revised by
the [2026-06-07 addendum](#addendum-2026-06-07--confirmed-safari-blocker--native-architecture)
(native-owns-auth/tokens/ingest after the Origin blocker was proven on-device)
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
> **⚠️ Superseded — see the [2026-06-07 addendum](#addendum-2026-06-07--confirmed-safari-blocker--native-architecture).**
> The browser-only device flow below was **disproven on Safari** (Auth0 403s the
> extension `Origin`, which Safari cannot strip). Auth must run in the **native
> app**, and tokens live in the **Keychain**, not `browser.storage.local`.

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

## Addendum (2026-06-07) — confirmed Safari blocker + native architecture

The original Sub-project 2 above assumed the iOS/Safari shell could run the
**same browser-only device flow** from the extension (open
`verification_uri_complete`, poll `/oauth/token`, store tokens in the
extension's `browser.storage.local`). **That assumption was disproven by a live
test.** This addendum records the proven blocker and the architecture that
replaces the stale parts of "Sub-project 2 — Mobile Safari shell" and "Auth on
iOS" above.

### The Origin blocker (proven live, not assumed)

Auth0 **403s the request when it carries the extension's `Origin` header.**
- **Chrome** strips `Origin` from the extension's auth requests via
  `declarativeNetRequest` (`modifyHeaders`), so the device flow works from the
  background service worker.
- **Safari cannot.** `Origin`/`Host` are *disallowed sensitive headers* a web
  extension may not set or remove; Safari's `declarativeNetRequest`
  `modifyHeaders` does **not** apply to extension-initiated (fetch) requests;
  and `chrome.identity` is unsupported. There is no extension-side path to send
  the Auth0 request without the rejected `Origin`.
- **Verified both directions on-device:** a converted Safari extension `fetch`
  to `/oauth/device/code` → **403**; the identical request from native Swift
  `URLSession` (no extension `Origin`) → **200**.

**Consequence:** on Safari, **auth cannot live in the extension's JS.** It must
live in the **native app**, which is the one process that can talk to Auth0
without a rejected `Origin`.

### Native-owns-auth/tokens/ingest (the chosen architecture)

This supersedes "Auth on iOS" and the `browser.storage.local` token note above.

1. **Native owns auth.** The containing app runs the Auth0 device flow via
   `URLSession` (no callback URL, no new Auth0 app needed). Chosen over
   `ASWebAuthenticationSession` + PKCE precisely because device flow needs no
   redirect URI. *(Shipped: `AuthManager.swift`, PR #91.)*
2. **Tokens in the Keychain, device-local.** `kSecAttrAccessibleAfterFirstUnlock
   ThisDeviceOnly` (NOT iCloud-synced). *(Shipped: `KeychainStore.swift`,
   PR #91.)*
3. **Native does ingest** so tokens never enter JS. The native side ports the
   wire transform and the def/tag resolver:
   - wire byte-parity transform → `Wire.swift` *(PR #93, merged; 30/30 golden vectors after review)*;
   - def/tag resolver → `EnsureDefinition.swift` *(PR #94, merged; 72/72 parity after review)*.
4. **Capture stays in JS** (visibility-based content script, `visibility.ts`,
   PR #87) and hands `AttentionEvent` batches to native via
   `sendNativeMessage` → `SafariWebExtensionHandler.swift`, which builds the
   wire record (Wire.swift), resolves the destination (EnsureDefinition.swift),
   and POSTs `ingest/v1/record/batch` with the Keychain token. The handler
   returns auth state (signed-in? which account?) so the popup can prompt
   sign-in without ever holding a token.
5. Safari uses **event pages**, not service workers; no `chrome.idle` /
   `chrome.windows` focus / `chrome.history` → **no backfill**, visibility +
   opportunistic `pagehide` flush only.

### Sharing layer — App Group + Keychain access group (implemented, needs portal registration)

The extension process (where `SafariWebExtensionHandler` runs) must read what
the app stored. Two **separate** entitlements are required — an App Group
identifier *cannot* double as a keychain access group:

| Shared thing | Mechanism | Identifier | Why |
|---|---|---|---|
| Resolved `{definitionId, tagIds}` (non-secret) | **App Group** shared `UserDefaults` suite | `group.com.fulcra.attention` | `EnsureDefinition`'s cache already takes a `UserDefaults(suiteName:)` hook; resolve once, both processes read it. |
| Access **token** (secret) | **Keychain access group** | `$(AppIdentifierPrefix)com.fulcra.attention.shared` | Shared `UserDefaults` is **unencrypted** — secrets must go in a shared Keychain group, same team, exact-same string on both targets. |

Both capabilities go on each app/extension pair (macOS app + macOS extension;
iOS app + iOS extension). Concretely:
- Add `keychain-access-groups` (value
  `$(AppIdentifierPrefix)com.fulcra.attention.shared`) to both targets'
  entitlements; set `KeychainStore`'s `kSecAttrAccessGroup` to the resolved
  runtime group (`<TeamID>.com.fulcra.attention.shared`). On macOS shared
  keychain queries also set `kSecUseDataProtectionKeychain` so the access group
  applies to the data-protection keychain.
- Add `com.apple.security.application-groups`
  (`group.com.fulcra.attention`) to both; point the resolved-id cache at
  `UserDefaults(suiteName: "group.com.fulcra.attention")`.

PR #97 wired this layer in code (`Sharing.swift`, the app/extension
entitlements, and the opt-in `KeychainStore(accessGroup:)` path).

**RESOLVED for macOS, verified 2026-08-12 — the registration is DONE.** This was
carried as "blocked on the operator" for days on the strength of the note below;
checking it took one signed build. All four targets declare both entitlements,
`DEVELOPMENT_TEAM` is `CWH48N2H7F` (matching `Sharing.teamIdentifierPrefix`), and
a signed macOS build succeeds against a real "Mac Team Provisioning Profile".
The signed artifacts carry, in both the app and the extension:

    com.apple.security.application-groups : group.com.fulcra.attention
    keychain-access-groups                : CWH48N2H7F.com.fulcra.attention.shared

`$(AppIdentifierPrefix)` resolves to exactly the group
`Sharing.keychainAccessGroup` computes, so the shared-keychain path is live on
macOS rather than merely wired.

**Still unproven on iOS**, and not because of the capability: this host has the
iOS 26.5 SDK but only the 26.2 and 26.4 simulator runtimes, so the iOS app target
cannot finish a build here at all. That is a missing platform component, not a
signing gap.

**Human step / why this can't be proven solely headless:** both identifiers must be
registered in the Apple Developer portal. With Automatic signing, Xcode
auto-registers them and regenerates the provisioning profiles **on the first
GUI build after the capability is enabled** — but a headless `xcodebuild` build
**fails code-signing** until that profile exists. So the entitlements can be
linted and typechecked, but the signed sharing behavior still needs Ash to
enable the two capabilities once in Xcode (Signing & Capabilities → + App
Groups, + Keychain Sharing) so automatic signing registers them, then it builds
headlessly thereafter. Team: `CWH48N2H7F`.

> Sources for the sharing mechanics: Apple,
> [Sharing access to keychain items among a collection of apps](https://developer.apple.com/documentation/security/sharing-access-to-keychain-items-among-a-collection-of-apps)
> (keychain-access-groups entitlement, `$(AppIdentifierPrefix)` value format,
> App-Group-≠-keychain-group; on macOS shared access uses the data-protection
> keychain); App Groups give a shared, **unencrypted** `UserDefaults`/container,
> so secrets belong in the shared Keychain group.

### Testing gap found 2026-08-10 (blocks confidence in 7 and 8)

The Xcode project has **no test target**, and every `.swift` is wired to a target
explicitly (no synchronized folders). So `FulcraAttentionTests/*.swift` compiled
nowhere and `EnsureDefinitionTests.swift` had never run since it landed. No CI
workflow invokes `xcodebuild` either, so nothing in `safari/` had ever been built
by CI.

Interim fix: `safari/scripts/run_swift_tests.sh` assembles the platform-agnostic
sources + the XCTest files into a throwaway SwiftPM package and runs
`swift test` — no Xcode project, no signing, so it works headlessly and in CI.
It runs 41 tests today (the previously-stranded `EnsureDefinitionTests` plus the
new ingest/sent-set suites).

Still owed: a real test target in the xcodeproj, which is the only way to cover
what needs the app sandbox (Keychain access groups, App Group container), and a
CI job that builds this project at all.

### Revised sequencing (native track)

1. ✅ Native auth (device flow via URLSession) + device-local Keychain — **PR #91 (merged)**.
2. ✅ Wire byte-parity transform → `Wire.swift` — **PR #93 (merged)**.
3. ✅ Def/tag resolver → `EnsureDefinition.swift` — **PR #94 (merged)**.
4. ✅ JS visibility capture → `visibility.ts` — **PR #87 (in review)**.
5. ✅ / pending profile **Sharing layer** (App Group + Keychain access group) — code landed in
   **PR #97**; still needs Ash's one-time Xcode capability/profile
   registration before signed runtime sharing is proven.
6. ✅ **Native ingest poster** (URLSession → `ingest/v1/record/batch`, refresh
   on 401) — `Ingest.swift` (`RelaylessSender`) + `SentSet.swift`, ported from
   `relaylessSender.ts` / `sentSet.ts`, composing Wire.swift +
   EnsureDefinition.swift's `TokenProvider`/`HTTPClient` seams.
   Claim-then-record: ids are marked sent only after a 2xx, so a failed POST
   retries instead of dropping events.
7. ✅ / JS half pending **nativeMessaging bridge** — `NativeBridge.swift`
   defines the wire protocol and owns every decision; the handler is a thin
   adapter (logic inside `beginRequest` is untestable by construction). The
   native sources are now in BOTH extension targets — they previously compiled
   only `SafariWebExtensionHandler.swift`, so none of this code existed in the
   extension process at all.

   The TypeScript **transport** now exists —
   `chrome/src/relayless/nativeTransport.ts`, sharing the SNAKE_CASE wire keys
   `AttentionEvent` already uses in `chrome/src/types.ts` (not the Swift
   property names). Outbox entries are handed across unchanged; a mapping layer
   is exactly where a renamed field would go quietly missing.

   **RESOLVED 2026-08-12 — separate Safari bundle** (operator decision; the
   criterion was standalone distribution, not build tidiness). `vite.safari.config.ts`
   + `manifest.safari.config.json` emit `chrome/dist-safari`, and the Xcode
   extension target copies THAT instead of `chrome/dist`. Measured on the
   emitted manifests: permissions **11 -> 3** (`storage`, `alarms`,
   `nativeMessaging`), host_permissions **2 -> 0**, and the all-URLs
   `optional_host_permissions` is gone. The bundle contains no reference to
   `api.fulcradynamics.com`, Auth0, the daemon port, `chrome.history`, or the
   ingest endpoint — verified by scanning both bundles, with the same scan
   finding them in the Chrome one.

   Honest caveat: capture must observe pages, so `content_scripts` still matches
   `http://*/*` and `https://*/*`. That is broad injection access granted at
   install. The reduction is real but it is 11 -> 3 permissions, not "no
   permissions".

   The entry points are `src/safari/{content,background,outbox,popup}.ts(x)`.
   The content script is the first production caller of
   `startVisibilityCapture`; it forwards visits to the background worker because
   content scripts cannot call `sendNativeMessage`.

   **Superseded below** — the three facts that forced this decision, kept for
   the record:

   - `startVisibilityCapture` has **no production caller** — it is invoked only
     from its own tests, so Safari capture runs nowhere today;
   - there is **no Safari build variant**: no separate manifest, no vite
     `define`, no build flag distinguishing a Safari bundle from the Chrome one;
   - the Xcode extension target copies `chrome/dist` **verbatim**, so the Safari
     extension currently ships the Chrome bundle.

   So finishing the JS half needs a packaging decision — a separate Safari
   bundle, or one bundle that picks its transport at runtime — and that decision
   changes what the extension target copies. It is deliberately not made here.

   **Runtime is still gated on step 5's portal registration.** The extension
   process reads the token from the shared Keychain group, so until that
   capability is registered the bridge answers `needs_sign_in` — truthfully,
   rather than crashing or reporting a false success. That path is tested.
8. ⏳ **iOS target** + content-script wiring + TestFlight (Ash's App Store
   Connect; paid Individual account exists).

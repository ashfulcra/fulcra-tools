# Fulcra Attention — v1 Design Spec

**Goal:** Capture what takes the user's attention while browsing — what they read, what they paid attention to — into the user's own Fulcra account as DurationAnnotations, so they can later recall "what was that article I read on Tuesday?"

**Threat model:** This is a **personal memory system**, not surveillance defense. The user is the only consumer of the data. Privacy posture defends against *accidental leakage* (auth tokens in URLs) and *user-chosen redaction* (specific sites the user doesn't want remembered) — not against the user themselves reading their own data.

**Status:** Design spec. Implementation deferred to a separate plan via `writing-plans`.

**Repo (to be created):** `ashfulcra/fulcra-attention` (separate from `fulcra-media`).

**Predecessor docs:**
- `2026-05-18-fulcra-browse-extension-auth0-app.md` — Auth0 app spec for v2 distribution.

---

## 1. Architecture

```
┌─────────────────────────┐         ┌──────────────────────────┐         ┌─────────────────┐
│ Chrome extension (MV3)  │ ──HTTP─►│ fulcra-attention relay   │ ──HTTP─►│ Fulcra Life API │
│ TypeScript + Vite       │  bearer │ Python, 127.0.0.1:8771   │  Bearer │  (cloud)        │
│                         │  token  │                          │  Auth0  │                 │
│ Capture: webNavigation  │         │ Endpoint: POST /attention│  token  │ /ingest/v1/...  │
│ Enrichment: content     │         │ Translates → Duration-   │         │                 │
│ script reads OG meta    │         │ Annotation, calls Fulcra │         │                 │
│ Scrub: Tier 1 always-on │         │ via fulcra-api (shells   │         │                 │
│ Categorize/Ignore: user-│         │ out to `fulcra auth      │         │                 │
│ controlled, default off │         │ print-access-token`)     │         │                 │
└─────────────────────────┘         └──────────────────────────┘         └─────────────────┘
        in Chrome only                  same machine,                       api.fulcradynamics.com
        (per Chrome profile)            launchd / systemd
```

**Why two processes:**
1. The relay holds the Fulcra Auth0 access token via the existing `fulcra-api` CLI. The extension never sees it.
2. The relay is also where the ingest pipeline lives. Extension stays dumb: capture → scrub → post.
3. v2 can collapse this into direct-to-cloud by replacing the relay with `chrome.identity.launchWebAuthFlow`. The extension's capture/scrubbing code is unchanged.

**Different port from fulcra-media (8765 vs 8771)** so the two services coexist on one machine. Each has its own bearer token, its own launchd unit, its own state file.

**Loopback only.** Relay binds to `127.0.0.1:8771`. No LAN mode in v1. No cross-machine relay communication.

---

## 2. Capture model

### Triggers

The extension's MV3 service worker subscribes to:

- `chrome.webNavigation.onCommitted` (frameId === 0) — fires once per full-page load
- `chrome.webNavigation.onHistoryStateUpdated` (frameId === 0) — catches SPA route changes

Both events fire `handleNavigation(tabId, url, timeStamp)`.

### Engagement model: duration, not instant pings

When a navigation fires:
1. If there's an "active visit" already open on this tab, **close it**: compute `end_time = now`, emit a DurationAnnotation event with `{tabId, url, title, start_time, end_time, ...}`, and write to outbox.
2. Open a new active visit: `{tabId, scrubbed_url, start_time: now}`.
3. Also close the active visit on: `chrome.tabs.onRemoved`, `chrome.windows.onFocusChanged` going to `WINDOW_ID_NONE` (Chrome loses focus), and `chrome.runtime.onSuspend` (which fires only on extension disable/update, **not** on routine SW shutdown — MV3 service workers can die silently).

This gives real time-on-page per visit. A 3-second bounce off Twitter and a 12-minute Substack read are distinguishable in the data.

**Active-visit state lives in `chrome.storage.session`** — it survives service worker death within a session but resets on browser restart. **An active visit that is open when the browser is force-killed will be lost** (no end_time was ever computed). Acceptable for v1; v2 can add an alarm-driven checkpoint that emits a partial event every N minutes while a visit is open.

**Outbox vs active-visit:** the outbox holds *closed* events (with both start_time and end_time), pending POST to the relay. Active visits are open events still accumulating duration. They are separate stores.

### Pre-filters (drop before scrubbing)

- `frameId !== 0` (iframe nav)
- `tab.incognito === true` (defense in depth; manifest also omits `"incognito"`)
- URL scheme not in `{http, https}` (skip `chrome://`, `file://`, extension URLs)

### Three-tier scrubbing model

| Tier | Default | Action | Wire format |
|---|---|---|---|
| **1 — Param strip** | Always on; ~80 items | Remove auth/tracking params from query + fragment | `{url: "<scrubbed>", title, og_description, favicon_url, ...}` |
| **2 — Categorize** | **Empty by default**; user-driven, optional presets | Replace URL/title with category slug; no host or path leaks | `{category: "banking", url: null, title: null, ...}` |
| **3 — Ignore** | Empty by default; user-managed | Drop event entirely | (no POST) |

**Precedence (most-specific wins):** Tier 3 → Tier 2 → Tier 1.

### Tier 1 — Param strip (always on)

The denylist (case-insensitive, applied to both query and fragment):

**Auth-bearing:**
`access_token, id_token, refresh_token, code, state, nonce, client_secret, assertion, session, sid, sessionid, auth, authorization, token, apikey, api_key, key, signature, sig, hmac, X-Amz-Signature, X-Amz-Credential, X-Amz-Security-Token, Signature, Expires, password, pwd, pw, otp, magic, share_token, invite, confirmation_token, _csrf, csrf_token, xsrf, ticket, ott`

**Tracking (ClearURLs-style):**
`utm_source, utm_medium, utm_campaign, utm_content, utm_term, gclid, fbclid, msclkid, mc_eid, mc_cid, _hsenc, _hsmi, igshid, yclid, ref, ref_src, ref_url`

**One-click action tokens:**
`unsubscribe, unsub, verify, reset, confirm, activate`

**Whole URL fragment dropped by default** (covers OAuth Implicit Flow + Slack/Notion magic-share links). User can opt fragment-keep per-domain in v1.5 settings.

### Tier 2 — Categorize (user opts in)

A user-managed map from domain pattern → category slug. For matched events, the wire payload contains `category: "<slug>"` and `url: null`, `title: null`. No host, no path, no anything identifying.

**Category vocabulary (v1):**
`search, webmail, ai-chat, dm, doc-editor, reddit-thread, calendar, banking, brokerage, crypto, tax, healthcare, password-manager, mental-health, dating, adult, job-hunting`

User can add custom categories in v1.5.

**Optional presets** the user can one-click apply (each adds a curated set of mappings):
- Finance: chase, bofa, wells, citi, fidelity, vanguard, schwab, paypal, venmo, robinhood, coinbase, etc.
- Healthcare: mychart, athenahealth, goodrx, etc.
- AI chats: chatgpt.com, claude.ai, gemini.google.com, copilot.microsoft.com, perplexity.ai
- Adult: imported from oisd.nl adult feed
- (more in v1.5)

**Default: no presets applied.** Tier 2 is empty until the user chooses.

### Tier 3 — Ignore (user-managed)

User-managed domain blocklist. Matched events are dropped entirely (no POST).

**Matching rules:**
- Exact host match: `example.com` matches only that host.
- Wildcard subdomain: `*.example.com` matches subdomains but not the apex (user can add both).
- No regex / no path patterns in v1.

**Storage:** `chrome.storage.sync` — propagates across Chrome profiles via Google sync. Other settings stay in `chrome.storage.local` (per machine).

### Title and enrichment scrubbing

When a domain is Tier 2, title is replaced with the category slug. When Tier 3, no event is emitted.

When Tier 1, title is preserved. **og_description, favicon_url, and content excerpt** (v2) are captured via a content script that runs only on commit/historyStateUpdated, only on the active tab, only after pre-filter pass.

---

## 3. Wire format and ingest

### Extension → Relay

```http
POST http://127.0.0.1:8771/attention
Authorization: Bearer <machine-local-token>
Content-Type: application/json

{
  "url": "https://example.com/article",        // null when categorized
  "title": "Why I Quit Twitter",               // null when categorized
  "og_description": "A 2026 reflection on...", // null when missing or categorized
  "favicon_url": "https://example.com/fav.ico",// null when missing or categorized
  "category": null,                             // string slug when categorized

  // Context enrichment (all optional; null when unknown)
  "chrome_identity": "redacted@users.noreply.github.com",  // Google account in this Chrome profile,
                                                 // OR a user-set label ("Work"/"Personal"/...)
                                                 // OR null when neither is available
  "og_type": "article",                         // <meta property="og:type">
  "lang": "en",                                  // <html lang="...">

  "start_time": "2026-05-18T14:23:08.412Z",
  "end_time":   "2026-05-18T14:35:42.108Z",
  "client": "fulcra-attention-chrome/0.1.0"
}
```

### Chrome identity capture

Users typically have multiple Chrome profiles for distinct contexts — e.g. one per client/company they work with, plus a personal profile. Concrete example:

| Chrome profile | Google account signed in | `chrome_identity` value |
|---|---|---|
| Fulcra (employer) | `redacted@users.noreply.github.com` | `redacted@users.noreply.github.com` |
| Acme Corp (consulting) | `ash@acmecorp.com` | `ash@acmecorp.com` |
| BetaCo (consulting) | `ash@betaco.com` | `ash@betaco.com` |
| Personal | `ash.personal@gmail.com` | `ash.personal@gmail.com` |
| Side project (no Google sign-in) | — | (user-set popup label, e.g. `"OSS"`) |

The extension fills `chrome_identity` from one of two sources, in order:
1. `chrome.identity.getProfileUserInfo({accountStatus: 'ANY'})` — returns the email of the Google account signed into this Chrome profile. Most users with multiple work contexts use distinct Google accounts per company; the email naturally encodes the company via its domain, so no extra config is needed for that case.
2. If empty (profile not signed into Google), a **free-text user-configurable label** in the popup. Persisted in `chrome.storage.local`. Defaults to null; user types whatever string makes sense for them (`"OSS"`, `"Open-source side-project"`, `"Volunteer board"`, etc.). Not constrained to an enum.

The relay treats `chrome_identity` as an opaque string. No server-side validation. Querying by company at retrieval time is straightforward: group by `external_ids.chrome_identity` exact match, or by email-domain substring for "show me everything under @acmecorp.com last week" use cases.

### Contextual tags policy

Static server-side tags (created at bootstrap, applied to every event): `attention`, `web`. No more — Fulcra tags require pre-created UUIDs, so dynamic-value tags (like one per host or one per language) would explode the namespace.

Dynamic context lives in `external_ids` (free-form key/value), where it's queryable but doesn't require server-side schema. The four enrichment fields above (`chrome_identity`, `og_type`, `lang`, `host`) all land there.

**Validation at relay:**
- Bearer token matches `~/.config/fulcra-attention/relay.json`
- Exactly one of `{url, category}` is non-null
- `start_time` ≤ `end_time` ≤ now + 5 min (clock skew tolerance)
- All other fields optional

### Relay → Fulcra (DurationAnnotation)

The relay converts the ping to a `DurationAnnotation` payload under the `Attention` definition with tags `[attention, web]`.

```json
{
  "specversion": 1,
  "data": {
    "note": "Attention: Why I Quit Twitter",            // or "Attention: banking" for categorized
    "title": "Why I Quit Twitter",                       // category slug if categorized
    "service": "web",
    "category": null,                                    // category slug if Tier 2 hit
    "url": "https://example.com/article",                // null if categorized
    "og_description": "A 2026 reflection on...",
    "favicon_url": "https://example.com/fav.ico",
    "parent_source_id": null,                            // reserved for v2 highlights
    "external_ids": {
      "client": "fulcra-attention-chrome/0.1.0",
      "host": "example.com",                             // null if categorized
      "chrome_identity": "redacted@users.noreply.github.com",       // null if unavailable
      "og_type": "article",                              // null if missing
      "lang": "en"                                       // null if missing
    }
  },
  "metadata": {
    "data_type": "DurationAnnotation",
    "recorded_at": {
      "start_time": "2026-05-18T14:23:08Z",
      "end_time":   "2026-05-18T14:35:42Z"
    },
    "tags": ["<attention-tag-uuid>", "<web-tag-uuid>"],
    "source": [
      "com.fulcra.attention.v1.<sha256(url_or_category|start_time_to_second)[:16]>",
      "com.fulcradynamics.annotation.<attention-def-uuid>"
    ],
    "content_type": "application/json"
  }
}
```

**Source-id idempotency:** Re-posting the same event (e.g. retry from outbox) is a silent no-op on the Fulcra side. Same scheme as fulcra-media's importers.

**Watermark:** Relay tracks the max `end_time` posted per-client in `state.json["watermarks"]["<client>"]`. Used only for status reporting in v1; in v2 enables `--since` queries.

---

## 4. Components

### `fulcra_attention/relay.py`

Stdlib `http.server.ThreadingHTTPServer` bound to `127.0.0.1:8771`. Single route: `POST /attention`. Mirrors `fulcra_media.webhook` exactly — bearer validation, JSON parsing, hand-off to ingest.

### `fulcra_attention/ingest.py`

`build_attention_event(payload)` → `DurationAnnotation` dict (the JSON above). `ingest_batch(events)` → POST to Fulcra `/ingest/v1/record/batch` (reuses `fulcra_attention.fulcra.FulcraClient`, which mirrors `fulcra_media.fulcra.FulcraClient`).

### `fulcra_attention/fulcra.py`

Auth via `subprocess.run(["fulcra", "auth", "print-access-token"])` (same pattern as fulcra-media). `FULCRA_ACCESS_TOKEN` env override. Bootstrap creates the `Attention` definition + `attention` + `web` tags idempotently.

### `fulcra_attention/scrub.py`

`scrub_url(url) -> str` applying the Tier 1 denylist to query + fragment. Pure function. Property-tested.

### `fulcra_attention/cli.py`

```
fulcra-attention bootstrap         # create Attention def + tags
fulcra-attention setup              # generate bearer token, write launchd/systemd unit
fulcra-attention status             # print state.json contents
fulcra-attention reset              # soft-delete def, clear watermarks
fulcra-attention relay              # foreground-run the relay (used by launchd plist)
```

### `chrome/src/background.ts`

MV3 service worker. Owns:
- `chrome.webNavigation.onCommitted` + `onHistoryStateUpdated` subscribers
- Active-visit state machine (open on nav, close on nav/tab close/window blur/suspend)
- Outbox in `chrome.storage.local`
- `chrome.alarms` tick every 60s to retry failed posts

### `chrome/src/content.ts`

Tiny content script injected via `chrome.scripting.executeScript` on the active tab post-debounce. Reads:
- `document.title`
- `<meta property="og:description">`
- `<link rel="icon">` / `<link rel="shortcut icon">` resolved against base URL

Posts back to the SW via `chrome.runtime.sendMessage`.

### `chrome/src/scrub.ts`

Port of `fulcra_attention/scrub.py`. **Cross-language contract:** identical inputs produce identical outputs. Tested with shared fixture file.

### `chrome/src/categorize.ts` and `chrome/src/ignore.ts`

Tier 2 and Tier 3 lookups against `chrome.storage.local` (categorize) and `chrome.storage.sync` (ignore).

### `chrome/src/outbox.ts`

Write-ahead in `chrome.storage.local`. POST to relay; on success delete from outbox; on failure leave. Cap 5000 entries, oldest dropped at overflow.

### `chrome/src/popup/`

Tiny React app. Shows:
- On/off toggle
- Bearer token field
- Live last-5-events stream (post-scrub URLs as they'll land in Fulcra)
- Per-event buttons: "Ignore this domain" / "Categorize as: <dropdown>"
- "Ignore list" view: list of domains, add/remove
- Counts: today's logged / categorized / ignored

### `chrome/src/options/`

Full settings page (v1.5+). Tier 2 editor, preset import buttons, etc. v1 ships minimal placeholder.

---

## 5. Per-machine install

```bash
# 1. Install relay + CLI
pipx install fulcra-attention

# 2. Authenticate to Fulcra (browser opens, OIDC device flow)
fulcra auth login

# 3. Bootstrap Attention def + tags (idempotent)
fulcra-attention bootstrap

# 4. Generate bearer token, register launchd / systemd service
fulcra-attention setup
#   prints: "Bearer token: 4f2a8b...  (paste into extension popup)"

# 5. Load Chrome extension (unpacked from release zip, or unlisted CWS)

# 6. Open extension popup, paste bearer token, click Save
```

**Artifacts per machine:**
- `~/.config/fulcra-attention/state.json` — def UUID, tag UUIDs, watermarks
- `~/.config/fulcra-attention/relay.json` — bearer token, port (mode 0600)
- `~/Library/LaunchAgents/com.fulcra.attention.relay.plist` (macOS) or `~/.config/systemd/user/fulcra-attention-relay.service` (Linux)
- Chrome extension: per-Chrome-profile, bearer pasted per-machine. Ignore list propagates via `chrome.storage.sync`.

---

## 6. Privacy posture (explicit)

1. **Tier 1 always-on:** the user can never accidentally leak an OAuth callback URL containing an access_token. This is non-configurable.
2. **Tier 2 default off:** the user is not silently categorized into "you visited a finance site" — every category mapping is explicit and user-added.
3. **Tier 3 default empty:** no domain is hidden from the user's own memory without their action.
4. **Live popup preview** shows exactly what's being logged in real time, post-scrub. No hidden capture.
5. **Loopback-only relay** means even a compromised LAN device cannot post fake attention events to Fulcra.
6. **Bearer-token between extension and relay** so a malicious page running a fetch to `http://127.0.0.1:8771/attention` cannot inject events (CSRF defense).
7. **No third-party services.** Extension talks only to `127.0.0.1`. Relay talks only to `api.fulcradynamics.com`. No telemetry.
8. **Incognito excluded** by manifest omission of `"incognito"` key. Chrome's default behavior is "extension cannot see private tabs."

---

## 7. Testing strategy

**Python relay + ingest (pytest, hermetic, `httpx.MockTransport`):**
- Relay endpoint contract: bearer enforcement, schema validation, malformed JSON, idempotency under replay.
- Scrub denylist: ~50 table-driven cases.
- Ingest payload shape: deterministic source-ids, exact `/ingest/v1/record/batch` byte-for-byte match.
- State bootstrap: idempotency, partial-state recovery.

**Chrome extension (Vitest + jsdom):**
- Scrub: port of Python test cases; cross-language byte-identical.
- Categorize / Ignore: precedence, wildcards.
- Outbox: write-ahead, retry, overflow.
- Background SW: mocked `chrome.webNavigation`, asserts correct active-visit transitions.

**No live-Fulcra CI tests.** Manual smoke test documented in README: real Chrome, real account, 5 representative URLs, verify Fulcra annotations match.

---

## 8. v2 plan (deferred, but designed-in)

| Feature | v1 ships | v2 adds |
|---|---|---|
| Auth | Localhost relay holds OIDC token via `fulcra-api` | Direct OAuth Authorization Code + PKCE via the Auth0 app spec |
| Distribution | Sideloaded unpacked / unlisted CWS for fulcra-org members | Public CWS listing |
| Tier 2 editing | View-only (no editor) | Full editor + presets + import/export |
| Enrichment | Title + OG description + favicon | Article body excerpt, schema.org keywords, reading-time estimate |
| Highlights | Schema includes `parent_source_id` (always null in v1) | Select text on page → annotate → land as separate `Highlighted` def, linked via `parent_source_id` |
| Retrieval | — | `fulcra-attention search`, `fulcra-attention recent`, popup search bar |
| Mobile | — | Safari Web Extension wrapper (separate repo, shared TS core) |
| Cross-machine LAN mode | — | Optional relay bind to LAN IP |

**Future-proofing decisions baked into v1:**
- Wire format identical to what the v2 direct-to-cloud path will send.
- Bearer-token-from-storage abstraction: in v2 the token source flips from popup-paste to OAuth result. Capture code unchanged.
- Auth0 app already spec'd (one app, many users), unblocked once Fulcra provisions.
- `parent_source_id` field reserved for v2 highlights.

---

## 9. Open questions

1. **InstantAnnotation vs DurationAnnotation:** v1 uses DurationAnnotation (we have real start/end times). Confirmed.
2. **Fulcra API support for DurationAnnotation with `category` field:** verify `fulcra-api` doesn't reject unknown `data` fields. If it does, `category` moves to `external_ids`.
3. **Bearer token rotation:** the per-machine bearer token is generated once at `setup` and never rotated automatically. Acceptable for v1 (loopback + user-only).
4. **Service-worker race conditions on rapid navigation:** mitigated via debounce + `chrome.storage.session` mutex; verify in manual smoke test.
5. **Chrome sync of Tier 3 ignore list across browsers (not just profiles):** depends on user's Chrome sync settings; documented in README, not enforced.

---

## 10. Out of scope for v1

- Highlights (text selections → annotations)
- Article body extraction / readability mode
- Retrieval UI (search, recent)
- Mobile (Safari, Firefox)
- LAN-mode relay
- Auth0 OAuth in the extension (deferred to v2 via the separate Auth0 spec)
- Custom Tier 2 categories beyond the v1 vocabulary
- Schema.org / `<meta keywords>` topic extraction
- Cross-device watermark / replay (each machine is independent)
- Public CWS distribution (v1 is unlisted / sideloaded)

---

## 11. Success criteria

After install on one machine, a user can:
1. Visit any web page → an `Attention` DurationAnnotation appears in their Fulcra account within 5 seconds of leaving the page (next nav, tab close, or 60s alarm flush).
2. Visit a URL containing `?access_token=...` → the access token is stripped before logging.
3. Click "Ignore this domain" in the popup → no future events from that domain land in Fulcra.
4. Open the popup → see the last 5 captured events as they will appear in Fulcra (post-scrub).
5. Re-install / re-import → no duplicate events (source-id idempotency).

After install on two machines:
6. The ignore list propagates from machine A to machine B via Chrome sync.
7. Annotations from both machines coexist in the same Fulcra account, distinguished by `external_ids.client` if needed.

# Streaming Media Capture Pathways — Research Report

> Compiled 2026-05-17 by a research subagent. This is the source-of-truth document for the [Last.fm + periodic architecture plan](../plans/2026-05-17-lastfm-and-periodic-architecture.md) and the future top-level `fulcra-media setup` decision tree. Section anchors are stable so the plan can link in.

For each service we evaluate the seven pathway options (Direct API, GDPR export, Pipedream, IFTTT, Local DB, Browser extension, Nothing viable), recommend an ongoing capture path and a backfill path, and call out caveats. Confidence is noted inline.

---

## 1. Video streaming

### Hulu

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No** | No consumer API for watch history; partner APIs are vendor-only |
| GDPR export | **Yes** (US State Privacy Rights) | Email-delivered report within 30 days, includes timestamps per title and device — one-shot only |
| Pipedream | **No** | No Hulu app in Pipedream's directory |
| IFTTT | **No** | Hulu is not a listed IFTTT service |
| Local DB | **No** | No desktop client with local state |
| Browser extension | **Partial** | Some Simkl/community tools scrape the "Keep Watching" rail |
| Nothing viable | — | Closest to reality for ongoing capture |

**Ongoing:** Browser/Simkl-style scraping is the only path; otherwise nothing. Recommend deferring ongoing capture and treating Hulu as backfill-only via the privacy export.
**Backfill:** Hulu US State Privacy Rights data request (30-day SLA). Confidence high.
**Caveats:** Hulu has actively been *removing* a user-facing watch-history page from the UI, which makes scraping more fragile. The privacy request is the only stable surface. Confidence high.

### Peacock (NBCUniversal)

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No** | None public |
| GDPR export | **Partial** | NBCUniversal honors CCPA/GDPR requests but no documented "watch history" line item — what you receive is account-scoped, not granular event log. Confidence medium. |
| Pipedream | **No** | Not in directory |
| IFTTT | **No** | Not in directory |
| Local DB | **No** | — |
| Browser extension | **No** | No active community tool found |
| Nothing viable | **Mostly** | — |

**Ongoing:** Nothing viable. Confidence high.
**Backfill:** File an NBCUniversal/Comcast privacy request and hope. Confidence medium.

### Disney+

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No** | Private API exists (unofficial GitHub libraries) but ToS-violating |
| GDPR export | **Yes** | Walt Disney US State Privacy Rights portal at privacy.thewaltdisneycompany.com handles DSARs |
| Pipedream | **No** | — |
| IFTTT | **No** | — |
| Local DB | **No** | — |
| Browser extension | **Partial** | A few scraper userscripts exist but ToS-risky |

**Ongoing:** Nothing viable; ToS bars the private-API approach.
**Backfill:** Disney privacy request via privacy.thewaltdisneycompany.com. Confidence high.

### Max (formerly HBO Max)

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No** | Confirmed no public API |
| GDPR export | **Partial** | WBD DSARs exist but quality of watch-history data is uneven; reports of redacted exports |
| Pipedream | **No** | — |
| IFTTT | **No** | — |
| Local DB | **No** | — |
| Browser extension | **Yes** | GreasyFork has an active "HBO Max Export watchlist" userscript (script ID 472776) — works on the My List + Continue Watching tabs |

**Ongoing:** Point user at the GreasyFork userscript and a generic-CSV pipeline. Confidence medium — userscripts break when Max ships UI changes.
**Backfill:** Same userscript snapshot; alternately WBD DSAR. Confidence medium.

### Amazon Prime Video

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No** | Prime Video APIs at videocentral.amazon.com are partner/vendor only; no consumer endpoint for viewing history |
| GDPR export | **Yes** | Amazon's data request portal (Your Account → Request My Data → "Prime Video") delivers viewing history as CSV/JSON via email, typically within a few days |
| Pipedream | **No** | — |
| IFTTT | **No** | Amazon-side IFTTT integrations are Alexa/shopping-focused |
| Local DB | **No** | — |
| Browser extension | **Yes** | `twocaretcat/watch-history-exporter-for-amazon-prime-video` scrolls the watch-history page and emits CSV |

**Ongoing:** Browser-exporter run on a cadence (manual, monthly). Or recommend nothing and rely on backfill snapshots only.
**Backfill:** Amazon "Request My Data" → Prime Video category. Confidence high.

### YouTube (regular videos)

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No** | YouTube Data API v3 explicitly disallows reading the user's watch-history playlist |
| GDPR export | **Yes** | Google Takeout → "YouTube and YouTube Music" → "history" → **JSON** produces `watch-history.json` with title, video URL, channel URL, ISO 8601 timestamps |
| Pipedream | **Partial** | YouTube app exists in Pipedream but no "new watch" trigger — only "new video by channel"/"new subscription" etc. |
| IFTTT | **Partial** | YouTube is live but triggers are upload/like/subscription oriented, not history |
| Local DB | **No** | — |
| Browser extension | **Partial** | Community tools parse the Takeout HTML; not ongoing |

**Ongoing:** Google Takeout supports **scheduled exports** (every 2 months, up to 1 year). Have `fulcra-media-helpers` pick up the latest archive. Caveat: 2-month is the minimum cadence. Confidence high.
**Backfill:** Same Takeout JSON, one-shot. Confidence high.
**Caveats:** YouTube watch-history must be enabled at myactivity.google.com. Takeout data lacks watch *duration* and watch position; just title + timestamp.

### Paramount+

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No** | Marketing/CMS API endpoints exist but nothing for user watch history |
| GDPR export | **Partial** | Paramount Global has a privacy request portal but reports of watch-history granularity are weak |
| Pipedream | **No** | — |
| IFTTT | **No** | — |
| Local DB | **No** | — |
| Browser extension | **No** | None known |

**Ongoing:** Nothing viable. Confidence high.
**Backfill:** Paramount Global privacy request. Confidence low (uncertain whether watch history is included).

### Apple TV+

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No** | No official Apple TV+ playback-history API |
| GDPR export | **Yes** | privacy.apple.com Data & Privacy portal — "Apple Media Services information" includes Apple TV app activity. EU/UK/JP users can schedule recurring exports |
| Pipedream | **No** | — |
| IFTTT | **No** | — |
| Local DB | **Partial** | macOS TV.app maintains some local state (already covered by `apple-takeout` importer) |
| Browser extension | **No** | — |
| **Trakt scrobbler** | **Yes** (Universal Trakt Scrobbler browser extension) | UTS explicitly supports tv.apple.com playback scrobbling to Trakt |

**Ongoing:** **Use Trakt.** Install Universal Trakt Scrobbler in the user's browser; UTS detects tv.apple.com playback and pushes start/pause/stop events to Trakt; the existing Trakt importer catches it. Confidence high — this is the canonical path and matches the repo's existing Trakt + Apple Takeout splits.
**Backfill:** privacy.apple.com data request (one-shot, slow). Confidence high.

---

## 2. Music streaming

### Apple Music (recent plays, not HealthKit)

| Pathway | Status | Note |
|---|---|---|
| Direct API | **Yes** | `GET /v1/me/recent/played/tracks` — requires MusicKit |
| GDPR export | **Yes** | privacy.apple.com → "Apple Media Services information" |
| Pipedream | **No** | — |
| IFTTT | **No** | — |
| Local DB | **Partial** | macOS Music.app stores some plays in a local SQLite |
| Browser extension | **No** | — |

**Ongoing:** Direct API — but with friction. Requires (a) Apple Developer Program membership ($99/yr), (b) Music-User-Token from MusicKit JS/iOS SDK (token doesn't get issued via plain web OAuth), (c) developer token re-signing every 6 months. The endpoint returns a *deduplicated* recent list (not full play history) — repeat plays within the window collapse. Confidence high.
**Backfill:** privacy.apple.com Data & Privacy export. Confidence high.
**Caveats:** "Recently played" returns the last N **distinct** items, not every individual play event. For a timeline this is acceptable; for accurate play counts it isn't.

### YouTube Music

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No** | No official YouTube Music API |
| GDPR export | **Yes** | Google Takeout → "YouTube and YouTube Music" → music-history.json |
| Pipedream | **No** | — |
| IFTTT | **No** | — |
| Local DB | **No** | — |
| Browser extension | **Partial** | `sigma67/ytmusicapi` (Python, unofficial, cookie-based) supports `get_history()` |

**Ongoing:** `ytmusicapi` with cookie-based auth, polled hourly. Brittle but works. Confidence medium.
**Backfill:** Google Takeout JSON. Confidence high.

### Tidal

| Pathway | Status | Note |
|---|---|---|
| Direct API | **Partial** | TIDAL Developer Portal provides OAuth 2.1 + PKCE but the official public API mostly exposes catalog/search, *not* personal "recently played" |
| GDPR export | **Yes** | Email privacy@tidal.com for DSAR |
| Pipedream | **No** | — |
| IFTTT | **No** | — |
| Local DB | **No** | — |
| Browser extension | **No** | — |

**Ongoing:** Scrobble Tidal to Last.fm (Tidal has first-class Last.fm support in settings), then capture via Last.fm API. This is what the user-facing tutorials all recommend. Confidence high.
**Backfill:** Tidal DSAR for full history; use Last.fm for ongoing. Confidence high.

### SoundCloud

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No (effectively)** | API exists but **new client_id registration has been closed since 2022**; the public dev portal requires Artist Pro and is dormant |
| GDPR export | **Yes** | DSAR via SoundCloud privacy contact |
| Pipedream | **Partial** | SoundCloud is "Action only" — no ongoing "new play" trigger |
| IFTTT | **No** (discontinued) | SoundCloud was removed from IFTTT |
| Browser extension | **No** | — |

**Ongoing:** Pipe SoundCloud → Last.fm via Web Scrobbler browser extension, then capture from Last.fm. Confidence medium.
**Backfill:** GDPR. Confidence medium.

### Bandcamp

| Pathway | Status | Note |
|---|---|---|
| Direct API | **Partial** | bandcamp.com/developer exposes Account/Sales/Merch APIs under OAuth 2.0, but **not** user listening history — those endpoints are sales/artist-side |
| GDPR export | **Partial** | DSAR returns purchase history. There's no "listening history" because Bandcamp doesn't track plays as identity-linked events for buyers |
| Pipedream | **No** | — |
| IFTTT | **No** | — |
| Browser extension | **No** | — |

**Ongoing:** Nothing viable for *plays*. For *purchases*, scrape the user's collection page (`bandcamp.com/<user>/purchases`) on a cadence. Confidence high.
**Backfill:** Same scrape. Confidence high.
**Caveats:** Bandcamp's mental model is "owned collection," not "listening events." Use a different paradigm — treat each new purchase as a one-off media-acquisition annotation, not a play event.

### Pandora

| Pathway | Status | Note |
|---|---|---|
| Direct API | **Partial** | developer.pandora.com exposes GraphQL with OAuth 2.0 but "recently played" queries are partner-tier — not individual-user OAuth |
| GDPR export | **Yes** | DSAR via Pandora privacy contact |
| Pipedream | **No** | — |
| IFTTT | **No** (discontinued) | Pandora is on the IFTTT discontinued-services list |
| Browser extension | **Partial** | Undocumented endpoints used by the web player at 6xq.net; ToS-risky |

**Ongoing:** **Pandora's web API has no public OAuth flow for non-business use — confidence high.** Either scrobble Pandora to Last.fm via Web Scrobbler, or treat as not viable.
**Backfill:** Pandora DSAR. Confidence high.

### Amazon Music

| Pathway | Status | Note |
|---|---|---|
| Direct API | **Partial** | Amazon Music Web API exposes `/v1/me/recentlyPlayedEntities` with Login With Amazon (LWA) bearer + x-api-key. However, LWA Security Profile registration for personal/non-Alexa use has historically been hard to get approved |
| GDPR export | **Yes** | Same Amazon "Request My Data" portal |
| Pipedream | **No** | — |
| IFTTT | **No** | — |
| Browser extension | **Partial** | Web Scrobbler supports music.amazon.com → Last.fm |

**Ongoing:** Best practical path is Web Scrobbler → Last.fm. If user can get LWA approval, direct API is cleaner. Confidence medium.
**Backfill:** Amazon "Request My Data" → Amazon Music. Confidence high.

### Deezer

| Pathway | Status | Note |
|---|---|---|
| Direct API | **Yes** | Deezer developer API at developers.deezer.com exposes `/user/{id}/history` via OAuth 2.0. Real, documented, personal-user endpoint with timestamps |
| GDPR export | **Yes** | Deezer support page documents the listening-history download |
| Pipedream | **No** | — |
| IFTTT | **No** | — |
| Browser extension | **Partial** | Web Scrobbler works |

**Ongoing:** **Direct Deezer API** with OAuth — best of any non-Spotify music service in this list. Confidence high.
**Backfill:** Paginate `/user/{id}/history` or use GDPR export. Confidence high.

---

## 3. Last.fm (deep dive)

Last.fm is the canonical scrobble-aggregator and ends up being the recommended ongoing pathway for many services above.

### Auth model — what to ask the user

Three distinct auth surfaces:

1. **Public reads (read-only)** — `user.getRecentTracks`, `user.getLovedTracks`, `user.getTopTracks` and all `user.get*` methods are explicitly marked "This service does not require authentication." **You only need an API key and the target username.** API key is free at https://www.last.fm/api/account/create (no review process).
2. **Scrobble-writing / love-marking** — `track.scrobble`, `track.love`, `track.unlove` require a session key via the web auth flow.
3. **Mobile auth** — deprecated, do not use.

**Recommendation:** Ask the user for (a) Last.fm username and (b) an API key from https://www.last.fm/api/account/create. No OAuth, no callback, no client secret.

### `user.getRecentTracks` — full schema

- **URL:** `https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks`
- **Required:** `user`, `api_key`, `format=json`
- **Optional:** `limit` (default 50, max **200**), `page` (1-indexed), `from` (Unix seconds), `to` (Unix seconds), `extended=1` (adds per-artist image/url and `loved=0|1`)
- **Response shape:**
  ```
  recenttracks: {
    "@attr": { user, totalPages, page, perPage, total },
    track: [
      {
        artist: { mbid, "#text" },
        album:  { mbid, "#text" },
        name:   "<track title>",
        mbid:   "<track MBID; may be empty>",
        url:    "https://www.last.fm/music/...",
        image:  [{size, "#text"}, ...],
        date:   { uts: "1715900000", "#text": "Mon, 17 May 2024 ..." },
        "@attr": { nowplaying: "true" }    // ONLY on currently-playing
      }
    ]
  }
  ```
- **Pagination:** `@attr.totalPages` says how many pages. Iterate `page=1..totalPages`. With `from`/`to`, you can get tight windows.
- **Nowplaying items:** Most-recent entry lacks `date` when user is currently playing something. **Filter these out** — they're not finished scrobbles. Detection: `track["@attr"]?.nowplaying === "true"` or missing `date`.

### Edge cases

- **Scrobble-age policy:** Last.fm doesn't accept new scrobbles older than **2 weeks** (write-side). On the read side, all historical scrobbles remain queryable via `from`/`to`.
- **Recent-window reordering:** Within ~30 days, scrobbles can occasionally get reordered as server-side merging runs. **Polling pattern:** don't use last-seen `uts` as a hard floor — overlap 24-48 hours on each poll and dedup on `(uts, artist, track)`.
- **Old-scrobble precision:** Pre-2014ish scrobbles imported from itunes XML or competitors' bulk imports may have minute-level precision (`uts % 60 == 0`).
- **Rate limit:** **5 req/sec per IP**, documented at last.fm/api/tos §4.4. Error code **29** = rate-limit exceeded. Backoff + sleep on 29.

### Other endpoints

- **`user.getLovedTracks`** — same auth; returns each loved track with `date.uts`. Useful as a separate annotation type ("Loved").
- **`user.getTopTracks`** — periodic aggregates (overall/7day/1month/3month/6month/12month). Not useful for event-level ingest.

---

## 4. Reading / podcasts / other

### Audible

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No** | `audible.readthedocs.io` documents reverse-engineered endpoints, requires username/password + custom device registration |
| GDPR export | **Partial** | Amazon "Request My Data" includes Audible Library and Listening Log |
| Local DB | **Partial** | macOS Audible.app stores some local state |
| Browser extension | **Yes** | "Audible Library Extractor" (modernnomad) userscript exports library + statistics |

**Ongoing:** Unofficial-API `audible` Python library is the most automatable. Confidence medium.
**Backfill:** Amazon "Request My Data" → Audible. Confidence high.

### Kindle (reading progress)

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No** | Unofficial: `Xetera/kindle-api`, `msuozzo/Lector`. Amazon DSARs do NOT include reading progress, only purchases + notes |
| GDPR export | **Partial** | Purchases + highlights only, no per-page-turn progress |
| Local DB | **Partial** | Kindle desktop app stores local progress on macOS |
| Browser extension | **Partial** | read.amazon.com cookie-scrape via `kindle-api` |

**Ongoing:** read.amazon.com scrape via `kindle-api`. Brittle but workable. Confidence medium.

### Letterboxd

| Pathway | Status | Note |
|---|---|---|
| Direct API | **Partial** | api-docs.letterboxd.com is the official spec but **closed beta — apply via email to api@letterboxd.com**. Explicitly **not granted for "private or personal projects"** |
| GDPR export | **Yes** | Settings → Import & Export → Export your data (CSV with diary entries, dates, ratings) |
| **RSS** | **Yes** | Every member profile has an RSS feed of new diary entries — `letterboxd.com/<user>/rss/` |

**Ongoing:** **RSS feed polling.** Most third-party Letterboxd integrations use this. Parse `<pubDate>` as the watch event timestamp. Hourly cadence is fine. Confidence high.
**Backfill:** CSV export. Confidence high.

### Goodreads

| Pathway | Status | Note |
|---|---|---|
| Direct API | **No (dead)** | Goodreads stopped issuing API keys on December 8, 2020. Existing keys still work in degraded form |
| GDPR export | **Yes** | Settings → Import and export → "Export Library" |
| **RSS** | **Yes** | Every shelf has an RSS feed: `goodreads.com/review/list_rss/<id>?shelf=read` |

**Ongoing:** **RSS feed of the "read" shelf.** Confidence high.
**Backfill:** Export Library CSV. Confidence high.
**Alternative:** Recommend the user migrate to **Hardcover** or **StoryGraph** (real APIs).

### Pocket — **DEAD**

| Pathway | Status | Note |
|---|---|---|
| **Direct API** | **Shut down** | **Pocket shut down on 2025-07-08; the API was disabled and user data export closed on 2025-11-12.** Confidence high |

**Status:** Service is dead. If user already migrated:
- **Readwise Reader** — documented REST API with OAuth + `GET /api/v3/list/?updatedAfter=...` (incremental). **Strong replacement; recommend if user wants ongoing capture.**
- **Instapaper** — documented OAuth 1.0a API (`/api/1.1/bookmarks/list`); slower-moving but works. Official Pocket replacement on Kobo e-readers.
- **Raindrop.io** — v1 REST API with OAuth 2.0.

If a user asks about Pocket, redirect to "which replacement did you migrate to?" and treat that as the integration question.

### Plex / Jellyfin (self-hosted)

| Pathway | Status | Note |
|---|---|---|
| **Direct API (Plex)** | **Yes** | Local API: `GET /status/sessions/history/all?X-Plex-Token=...` returns full play-event history with `viewedAt`, accountID, ratingKey, title, librarySectionID |
| **Webhook (Plex)** | **Yes** | Plex Pass users get webhooks: `media.play`, `media.pause`, `media.resume`, `media.stop`, `media.scrobble`. Tautulli adds the same for non-Pass users |
| **Direct API (Jellyfin)** | **Yes** | Full open REST API |
| **Webhook (Jellyfin)** | **Yes** | jellyfin-plugin-webhook emits `PlaybackStart`, `PlaybackProgress`, `PlaybackStop` |

**Ongoing:** **Webhooks.** This is the AI-agent-friendly path — set up a Plex/Jellyfin webhook pointed at a Pipedream/Cloudflare-Worker endpoint that translates events into Fulcra annotations. For Plex non-Pass users, use Tautulli's webhook agent.
**Backfill:** Plex `GET /status/sessions/history/all`; Jellyfin `/Users/{userId}/Items?Fields=UserData` and inspect `LastPlayedDate`. Confidence high for both.
**Caveats:** Plex webhooks require Plex Pass. Tautulli is the standard workaround.

### Strava

| Pathway | Status | Note |
|---|---|---|
| Direct API | **Yes** | OAuth 2.0, `GET /athlete/activities?after=<unix_ts>` is the canonical incremental endpoint |
| Webhook | **Yes** | Strava supports server-to-server webhooks for activity create/update/delete |
| **Pipedream** | **Yes — full** | "New Activity Created" trigger (webhook-backed) and "New Activity Updated" — Trigger + OAuth app |
| IFTTT | **Yes** | "New Activity" trigger |
| GDPR export | **Yes** | Settings → My Account → Download Your Data |

**Ongoing:** Direct API with webhook subscription, or Pipedream's first-class trigger. Confidence high.
**Backfill:** Paginate `GET /athlete/activities?after=0`. Confidence high.
**Caveats:** Initial sync window is documented as last 30 days on first connect. Token refresh every 6 hours.

---

## 5. Pipedream — apps with viable triggers (2026-05)

| Service | Trigger type | Notes |
|---|---|---|
| **Spotify** | New Saved Track ✓, New Track in Playlist ✓, New Playlist ✓, New Track by Artist ✓ — but **NOT** "new recently played track" | To capture plays via Pipedream, use scheduled workflow polling `GET /v1/me/player/recently-played`. Pipedream handles token refresh — that's the value-add. |
| **Strava** | New Activity Created ✓, New Activity Updated ✓, Custom Event ✓ | Full trigger app |
| **YouTube** | New Video by Channel, New Subscription, New Liked Video | No "new watch" trigger |
| **Last.fm** | Not in Pipedream's directory as a first-party app | Build a scheduled workflow polling `user.getRecentTracks` |
| **Goodreads** | Listed but Action-only / no useful native triggers | API is essentially dead |
| **Letterboxd** | Not in directory | Scheduled workflow polling the RSS feed |
| **Tidal/Deezer/Apple Music/YouTube Music/Pandora/Audible/Amazon Music/SoundCloud/Bandcamp** | None have first-party Pipedream apps | Action-only or absent |
| **Plex / Jellyfin** | Inbound-webhook style | Plex/Jellyfin posts to a Pipedream HTTP endpoint |

**Practical Pipedream recommendation for the CLI:** The "1-minute scheduled workflow writing rows to a CSV" pattern works best for **Spotify** (poll `recently-played`) and **Deezer** (poll `user.history`). For everything else either there's a real trigger (Strava), or no Pipedream app at all (Last.fm, Letterboxd) and you just build a scheduled workflow that hits the public API/RSS.

---

## 6. IFTTT — what's actually viable

IFTTT's media-streaming surface has thinned dramatically since 2021. As of May 2026:

| Service | IFTTT status |
|---|---|
| Spotify | **Live** — 9 polling triggers including `New recently played track` and `New saved track` |
| YouTube | **Live** — upload/like/subscription triggers, no "new watch" |
| Last.fm | **Partial / fragile** — old applets, no reliable "new scrobble" trigger today |
| Netflix | **Removed** |
| Hulu | **Not present** |
| Pandora | **Removed** |
| SoundCloud | **Removed** |
| Bandcamp / Apple Music / YouTube Music / Tidal / Deezer / Letterboxd | **Not present** |
| Strava | **Live** |
| Goodreads | **Removed** |
| Plex / Jellyfin | **Inbound webhook only via "Webhooks" service** |

**Polling cadence:** **5 minutes** for Pro/Pro+ users, **1 hour** for Free users. Free + Spotify's 50-track recently-played cap means a heavy-listener Free user *will* lose plays.

**Practical IFTTT recommendation:** Use IFTTT only for **Spotify** (`New recently played track` → Google Sheets row) and possibly **Strava**. Everything else, defer to direct API / Pipedream / Last.fm.

---

## 7. Final synthesis — one-page recommendation table

Sorted by recommended pathway quality (best to worst).

| Service | Ongoing recommendation | Backfill | Pathway tier | Confidence |
|---|---|---|---|---|
| Netflix | Existing CSV slim + GDPR rich importers | Same | Direct CSV + GDPR | High |
| Spotify | Existing Extended GDPR importer + Web API `recently-played` via Pipedream 1-min workflow | Spotify Extended Streaming History GDPR | Direct API (polled) | High |
| Trakt | Existing Trakt importer (also covers Apple TV+ via UTS browser extension) | Trakt API full history | Direct API | High |
| Apple Podcasts | Existing local-SQLite importer | Same | Local DB | High |
| Apple TV+ | Trakt + Universal Trakt Scrobbler browser extension | privacy.apple.com data request | Direct API (via Trakt) | High |
| **Last.fm** (aggregator) | **API key + username, poll `user.getRecentTracks` with `from`=last seen, dedup overlap, filter `nowplaying`** | Paginate to `from=0` | **Direct API** | **High** |
| Strava | Direct API + webhook subscription, OR Pipedream trigger | Same, paginate `after=0` | Direct API + webhook | High |
| Deezer | Direct API `user.history` via OAuth 2.0 | Same + GDPR | Direct API | High |
| Plex | Webhook (Pass) or Tautulli webhook → Pipedream → CSV | Local Plex SQLite + history endpoint | Webhook | High |
| Jellyfin | Webhook plugin → Pipedream → CSV | Jellyfin REST API `LastPlayedDate` | Webhook | High |
| Letterboxd | RSS feed polled hourly | Settings → Export CSV | RSS | High |
| Goodreads | RSS feed of "read" shelf | Settings → Export Library CSV | RSS | High |
| YouTube (videos) | Google Takeout scheduled export (2-month cadence) | Takeout JSON one-shot | GDPR export (scheduled) | High |
| YouTube Music | Google Takeout + optional `ytmusicapi` cookie scrape | Google Takeout `music-history.json` | GDPR export + scrape | Medium |
| Apple Music | Apple Music API (requires $99 Apple Developer + MusicKit user token); fallback to privacy.apple.com export | privacy.apple.com Apple Media Services | Direct API (high friction) | Medium |
| Amazon Prime Video | Browser exporter on cadence | Amazon "Request My Data" → Prime Video | Browser extension | Medium |
| Amazon Music | Web Scrobbler → Last.fm | Amazon "Request My Data" → Amazon Music | Sidecar to Last.fm | Medium |
| Tidal | Tidal → Last.fm (built-in setting) | Tidal DSAR | Sidecar to Last.fm | High |
| SoundCloud | Web Scrobbler → Last.fm | SoundCloud DSAR | Sidecar to Last.fm | Medium |
| Pandora | Web Scrobbler → Last.fm | Pandora DSAR | Sidecar to Last.fm / Nothing | Medium |
| Bandcamp | Scrape collection page for new purchases | Same | Browser/scrape | High |
| Audible | `audible` Python library; Amazon DSAR for backfill | Amazon "Request My Data" → Audible | Unofficial API | Medium |
| Kindle | `kindle-api` cookie scrape | Amazon DSAR (purchases + highlights only) | Browser/scrape | Medium |
| Max (HBO Max) | GreasyFork userscript on cadence | Same | Browser extension | Medium |
| Disney+ | None viable for ongoing | Disney privacy request | GDPR-only | High |
| Hulu | None viable for ongoing | Hulu US State Privacy Rights | GDPR-only | High |
| Peacock | None viable | NBCUniversal DSAR (uncertain quality) | GDPR-only | Low |
| Paramount+ | None viable | Paramount Global DSAR (uncertain quality) | GDPR-only | Low |
| **Pocket** | **Service shut down 2025-07-08; data export closed 2025-11-12** — redirect to Readwise Reader / Instapaper / Raindrop.io | n/a — window closed | Dead | High |

---

## Key cross-cutting findings

1. **Last.fm is the universal music-sidecar pathway.** For 7 of 8 music services (everything except Spotify which has its own API, and Bandcamp which is a different paradigm), the realistic ongoing-capture answer is "scrobble it to Last.fm, then poll Last.fm." Tidal has native Last.fm support; Amazon Music/SoundCloud/Pandora/YouTube Music rely on Web Scrobbler. **The Last.fm importer is the single most-leveraged piece of infrastructure** in this project after Spotify/Netflix/Trakt.

2. **Last.fm onboarding is dead simple.** Username + API key. No OAuth, no callback, no client secret. Far friendlier than every other "real" API in this list.

3. **RSS is underrated.** Letterboxd and Goodreads — two services with closed/dying APIs — both publish stable, free RSS feeds of user activity. **The CLI should grow a generic-RSS importer alongside the generic-CSV importer.**

4. **Video streaming is mostly hopeless for ongoing capture.** Netflix, Apple TV+ (via Trakt+UTS), and self-hosted Plex/Jellyfin are the only viable ongoing pathways. For everything else (Hulu, Disney+, Max, Prime Video, Peacock, Paramount+, YouTube), the realistic answer is "privacy request once or twice a year + Trakt for everything you'd otherwise lose." **Make Trakt scrobbling a first-class onboarding step that doubles as the catch-all for streaming-video gaps.**

5. **Pipedream's value is OAuth token management, not triggers.** Most music services don't have a "new play" Pipedream trigger; they have OAuth-managed credentials and you write a scheduled workflow.

6. **IFTTT is mostly dead for this domain.** Only Spotify and Strava are practically usable.

7. **Pocket is dead and gone.** Any user mentioning Pocket needs to be told they missed the export window (closed 2025-11-12) and asked which replacement they're on.

8. **Apple Music has an API but it's painful.** $99/yr Apple Developer + MusicKit-only user-token issuance. For most users, privacy.apple.com exports are the realistic answer; for power Mac users, the local Music.app SQLite is another (dovetails with Apple Podcasts).

9. **Deezer is the surprise winner for "real OAuth music API."** Real personal-user `user.history` endpoint with OAuth 2.0 and published Python library. Cleaner than Spotify (no 50-track per-call cap). Worth prioritizing.

10. **Self-hosted media servers (Plex/Jellyfin) deserve dedicated importers.** Webhooks are the AI-agent-friendly pattern. For a personal-data platform, "set up a webhook that posts every play event to Fulcra" is the cleanest possible integration story — recommend implementing this as a dedicated webhook receiver mode.

---

## Sources

- Last.fm API: [user.getRecentTracks](https://www.last.fm/api/show/user.getRecentTracks) · [auth spec](https://www.last.fm/api/authspec) · [TOS](https://www.last.fm/api/tos) · [rate limit thread](https://support.last.fm/t/api-rate-limit-for-user-api/112610) · [nowplaying behavior](https://support.last.fm/t/user-getrecenttracks-the-most-recent-track-will-not-include-a-date-field-if-it-is-currently-playing/115900)
- Spotify Web API: [recently played reference](https://developer.spotify.com/documentation/web-api/reference/get-recently-played)
- Apple Music API: [recent played tracks](https://developer.apple.com/documentation/applemusicapi/get-v1-me-recent-played-tracks)
- Trakt API: https://trakt.docs.apiary.io/ · UTS browser scrobbler
- Deezer API: https://developers.deezer.com/api
- Tidal Developer Portal: https://developer.tidal.com/documentation/api-sdk/api-sdk-authorization
- SoundCloud closed registration: GitHub issue tracker
- Pandora Developer: https://developer.pandora.com/docs/key-concepts/apis/
- Amazon Music Web API: https://developer.amazon.com/docs/music/API_web_overview.html
- YouTube Data API + Takeout: https://developers.google.com/youtube/v3/docs · https://takeout.google.com/
- Letterboxd: https://api-docs.letterboxd.com/ · https://letterboxd.com/api-beta/
- Pocket shutdown: lovable.dev/guides · mailist.app/blog
- Plex API + webhooks: plexopedia.com/plex-media-server/api · support.plex.tv/articles/115002267687-webhooks/
- Jellyfin webhook plugin: github.com/jellyfin/jellyfin-plugin-webhook
- Strava API: developers.strava.com · pipedream.com/apps/strava
- IFTTT Spotify triggers: ifttt.com/spotify · IFTTT discontinued services list
- Pipedream Spotify triggers: pipedream.com/apps/spotify
- Privacy/data export portals: privacy.apple.com · Amazon Request My Data · privacy.thewaltdisneycompany.com

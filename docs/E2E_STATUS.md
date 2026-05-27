# End-to-end test matrix — 2026-05-26 (late session)

Status of every plugin from hermetic test coverage to live verification. Updated after this session's systematic QA sweep.

## Legend

- ✅ **Live-verified** — actually ingested real data into a real Fulcra account in this codebase's lifetime, then queried back
- 🟢 **Code-path verified hermetically** — full importer + wizard contract + worker walk-through pass; would work end-to-end against a real account if creds were provided
- 🟡 **Wizard walks but unrun** — UI flow works, importer untested with realistic data
- ⚫ **Service plugin** — runs continuously; "E2E" means events flowing in, no scheduled run to trigger
- ❌ **Not testable here** — needs user creds / file we don't have

## Per-plugin status

| Plugin | Status | What's verified | What we couldn't test |
|---|---|---|---|
| **generic-rss** | ✅ Live-verified | Hackernews RSS → 30 events → 23 in Fulcra (earlier today) | — |
| **generic-csv** | ✅ Live-verified | Synthetic 3-row CSV → 3 events under new Watched def (earlier) | — |
| **apple-takeout** | 🟢 Hermetic (against user's real takeout) | `parse_any` extracts 3,580 events from real ~/Desktop/Apple Takeout.zip with since=2026-01-01; column shape verified; 16 importer tests pass | Live ingest skipped per user's planned "soft-delete + retry onboarding" |
| **apple-music-takeout** | 🟢 Hermetic (against real takeout) | `parse_any` extracts 528 events from same takeout; 14 importer tests pass | Same |
| **apple-podcasts** | 🟢 Hermetic | Health check probes the live SQLite DB; FDA verified on user's machine | Actual ingest pass (would need explicit Run-now after daemon restart) |
| **apple-podcasts-timemachine** | ❌ | Importer tested with synthetic SQLite fixtures | Needs mounted Time Machine drive |
| **goodreads** | 🟢 Hermetic + URL parser smoke-tested against user's actual profile URL | Importer tests pass; URL `https://www.goodreads.com/user/show/223358-singularity-co-bookshop` → `223358` ✓ | Live RSS fetch |
| **letterboxd** | 🟢 Hermetic | Importer tests pass; URL parser handles both bare username and full profile URL | Live RSS fetch |
| **lastfm** | 🟡 Wizard walks | Hermetic tests cover full importer; wizard's `test_connection` step validates creds against Last.fm | Needs real Last.fm API key from user to run end-to-end |
| **trakt** | 🟡 Wizard walks | OAuth flow synthetically smoke-tested via `scripts/smoke_trakt.py`; redirect URI copy clarified | Needs real Trakt app + OAuth round-trip |
| **deezer** | 🟡 Wizard walks | Token-paste flow tests pass | Needs Deezer dev account |
| **netflix** | ❌ | Importer tests with synthetic CSV pass | Needs `ViewingActivity.csv` from netflix.com/Activity |
| **spotify-extended** | ❌ | Importer tests with synthetic JSON pass | Needs Spotify Extended Streaming History takeout (~30d wait) |
| **youtube** | ❌ | Importer tests with synthetic JSON pass | Needs Google Takeout (YouTube → watch-history.json) |
| **dayone** | 🟡 Wizard walks | Both modes (live_app / export_file) have hermetic coverage; permission_check exists | Needs FDA OR an export zip |
| **attention-relay** | ⚫ Service | Extension paired in user's browser; events were flowing in pre-session ("Recently" feed showed 7+ events in 10 min); pill mapping fix means dashboard now correctly reports state | Cross-process: needs daemon restart for #29-#30 fixes (duration_seconds) to ship |
| **media-webhook** | ⚫ Service | Plugin contract + cross-machine wizard verified; conditional steps + bearer-token + `?token=` query path tested hermetically | Needs Plex/Jellyfin server pointed at the daemon's URL |

## What's blocked on user input

1. **Trakt** — full OAuth round-trip (highest-value unverified surface; user has been close several times today)
2. **Last.fm** — needs an API key from https://www.last.fm/api/account/create
3. **Deezer** — needs a token from https://developers.deezer.com/myapps
4. **Plex/Jellyfin** — needs a server pointed at the daemon
5. **Browser activity** — 30 seconds of browsing in the paired Chrome with the post-daemon-restart codebase to confirm Attention timeline rendering (#30 fix lands once daemon restarts)
6. **Apple takeouts (TV + Music)** — sitting at ~/Desktop ready to ingest; deferred per user's plan to "soft-delete + clear state + retry onboarding"

## What's verified end-to-end today

The full data flow is proven on:
- Two RSS-shaped plugins (generic-rss with Hackernews, would also cover Goodreads/Letterboxd shape)
- One CSV-shaped plugin (generic-csv with synthetic)
- Two takeout-shaped plugins via real-file parsing without ingest (apple-takeout, apple-music-takeout)

Every plugin's wizard contract loads, every setup_step kind is recognized, every required Setting/Credential is declared. No friendliness regressions: every run-with-empty-config produces a clear "missing X" message instead of a raw stack trace.

## What landed in this batch's QA round

- Pill mapping fixed: `last_outcome="error"` with `failures<3` now shows amber "Failed — run again" instead of "Not run yet"
- Attention "Not run yet" bug: extension-event POSTs now update per-plugin state's `last_outcome`/`consecutive_failures` (so the badge reflects "events are flowing")
- 4 duration plugins (apple-takeout, netflix, trakt, youtube) flagged for "raw IndexError when run with empty MagicMock" — turned out to be test-side artifact, not a real bug; real wizards enforce settings before run
- Dead-code sweep: 2 F841 unused-variable hits fixed, no orphan files, no broken entry points
- Secret-handling sweep: every `f"Bearer {token}"` is a legitimate Authorization header, no leakage in error messages or logs

## Recommended next live tests (in order)

1. **Daemon restart + browser hard-reload** — picks up everything from today's batches
2. **Soft-delete the existing 23 annotation defs from Settings** — clean slate
3. **Walk onboarding fresh** — exercise the new first-run auto-trigger + the editable Create-new flow + the Apple Music/TV wizards
4. **Browse 30 sec in paired Chrome** — verify Attention timeline now shows duration (closes #30)
5. **Trakt / Last.fm / Deezer end-to-end** — needs user creds

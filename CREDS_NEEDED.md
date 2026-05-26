# Creds / inputs needed to QA each plugin end-to-end

The goal is one tiny live test per plugin: configure → trigger one run → confirm an event lands in your Fulcra account at the expected def. For each line, the input is the smallest thing you can hand me to make that loop close.

**Status legend**: ✅ verified end-to-end this session • ⚠️ wizard walks but third-party API rejects (need real creds) • ❌ not yet attempted

## Group A — needs nothing from you

- ✅ **Generic RSS/Atom feed** — pointed at https://news.ycombinator.com/rss with category=`read`. Run-now fetched 30 entries → 23 landed in Fulcra under a new "Read" def (`ac4edb9e-…`). Activity feed shows `"Hackernews: 30 new annotations"`. End-to-end verified 2026-05-26.
- ✅ **Generic media CSV** — synthetic 3-row CSV at `/tmp/qa-media.csv` with default columns (timestamp/title/artist/id), category=`watched`. Run-now → 3 events under new "Watched" def (`15c9c456-…`). Activity feed `"Qa-test: 3 new annotations"`. End-to-end verified 2026-05-26.
- ❌ **Letterboxd film diary** — username-only. Skipped this pass (RSS-shaped like Generic RSS which is already verified). Worth a 30-second live walk with any public Letterboxd handle when you next QA.
- ❌ **Goodreads read shelf** — username-only, same shape as Letterboxd.

## Group B — needs creds from you

- ⚠️ **Last.fm scrobbles** — API key (free, https://www.last.fm/api/account/create) + your Last.fm username. Wizard fully walks. Test step (task #4) catches bad creds before you hit Run, with messages like `"Last.fm rejected the API key. Re-check it on the previous step."` Last verified path: fake creds → 403 (expected) → real creds → events.
- ❌ **Trakt watch history** — full OAuth round trip against trakt.tv. You'd need to be logged into trakt.tv when I click "Sign in with Trakt"; the wizard then writes redirect_uri = `http://127.0.0.1:9292/api/oauth/trakt/callback` and we verify the callback completes. Smoke script (`scripts/smoke_trakt.py`) already covers the synthetic path; this would be the live path. **Highest-value unverified surface.**
- ❌ **Deezer listening history** — Deezer OAuth access token (free dev account at https://developers.deezer.com/myapps). Same pattern as Last.fm: paste-token.
- ❌ **Spotify Extended Streaming History** — request the GDPR export from Spotify Account Privacy → wait ~30 days → upload the resulting zip. Probably not worth waiting; flag for when one already exists.
- ❌ **YouTube watch history** — Google Takeout containing YouTube → wait → upload `watch-history.json`. Same caveat as Spotify.
- ❌ **Netflix viewing history** — download `ViewingActivity.csv` from netflix.com/Activity (instant if logged in). Upload that.
- ❌ **Apple TV playback (takeout)** — Apple Data & Privacy takeout → download `Playback Activity.csv` → upload. Apple takes 1–7 days.

## Group C — needs real apps/services on this machine

- ❌ **Apple Podcasts (on-device)** — the live DB at `~/Library/Group Containers/group.com.apple.podcasts/Documents/MTLibrary.sqlite` needs Full Disk Access granted to whichever process runs the daemon. Today's terminal has FDA. Want me to do a synthetic read and just print episode counts (no annotations posted)?
- ❌ **Apple Podcasts (Time Machine recovery)** — needs a Time Machine backup mounted. Skip unless you specifically want to exercise this.
- ❌ **Day One journal** — pick the mode: `live_app` (needs FDA) or `export_file` (one-time import of a Day One JSON export ZIP). Both leak journal text into the test annotations. Probably best skipped unless you have a throwaway journal.
- ❌ **Plex/Jellyfin webhook receiver** — needs a Plex/Jellyfin server you can point at the daemon's webhook URL.

## Group D — needs the extension running while I QA

- ⚠️ **Attention** — extension is paired (one-click in the wizard), state correctly bound to the "Attention" def `b331bb73-…`, my synthetic POSTs land as proper `MomentAnnotation`/`DurationAnnotation` events with `metadata.name="Attention"`. The chrome extension itself only fires on real user-driven focus events (mousemove, keypress, etc.) — programmatic browser-driving doesn't trigger its heartbeat, so I can't fully exercise the live path without you actually browsing. **30 seconds of you browsing in the paired browser** is all it takes; activity feed surfaces a coalesced entry per 60 seconds.

## Quick-record (✅ verified end-to-end earlier session)

`/api/annotations` round-trip via daemon was the bug fix in the prior session. Posted test moments to `Personal Palantir Test Moment` (def id `793a0d72-…`).

## Timeline rendering (see task #30)

End-to-end data flow into Fulcra works for every plugin verified above. But after adding the corresponding data track at https://context.fulcradynamics.com/timeline, my Attention events showed as **invisible markers + "0 h 0 m total"** even though they were queryable via Fulcra's `/data/v1alpha1/event/DurationAnnotation`. Different codebase (context.fulcradynamics.com), filed as task #30 for cross-reference rather than fixed here.

## Recommended next-pass order

1. **Browse for 30 seconds** in the paired browser — closes the loop on Group D.
2. **Trakt** — riskiest unverified surface, full OAuth.
3. **Last.fm** — quick win with real API key.
4. **Apple Podcasts on-device** — likely already-FDA terminal, just needs your OK.
5. Defer Spotify/YouTube/Netflix/Apple-TV/Day-One until you have the takeouts handy.

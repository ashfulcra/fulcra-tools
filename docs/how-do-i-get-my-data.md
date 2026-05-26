# How do I get my data into Fulcra?

This page lists every data source Fulcra Collect can pull from today and
the pathways that exist for each one. Use it as a lookup: find the source
you care about, then pick the pathway that matches your setup.

Pathways fall into three rough buckets:

- **Live** — runs continuously. A browser extension, a webhook server, or
  an on-device file the daemon reads in place. New data lands without
  any further action.
- **Scheduled** — the daemon polls an API or RSS feed on an interval.
  Needs credentials (or just a username for the RSS-based plugins).
- **One-shot historical** — you download an export from the source
  (GDPR / takeout / activity dump), upload the file, and the importer
  parses it once. Re-run by uploading a fresher export.

Some sources have only one pathway; others have several (e.g. Apple
Podcasts can be read live from the on-device database AND recovered from
Time Machine snapshots). Where we know a pathway exists but Fulcra
Collect doesn't implement it yet, it's marked **Not yet supported** so
this doc doubles as a roadmap reference.

A few plugin IDs (`generic-rss`, `generic-csv`) are generic adapters
that can stand in for sources we don't have a dedicated plugin for —
worth a look when nothing else matches.

## Apple Podcasts

- **Live (on-device DB read)** — plugin `apple-podcasts`, scheduled
  every 6 hours. Reads the local Apple Podcasts SQLite database
  (`~/Library/Group Containers/*.podcasts*/Documents/MTLibrary.sqlite`)
  directly; the Podcasts app does not need to be open. Requires macOS
  **Full Disk Access** for the terminal/app running the daemon. Caveat:
  iCloud sync only keeps that DB fresh while macOS can run the Podcasts
  extension in the background — if you quit the app for days the DB may
  fall behind.
- **One-shot historical (Time Machine recovery)** — plugin
  `apple-podcasts-timemachine`, manual. Walks every Time Machine
  snapshot on the mounted backup volume and pulls played-episode rows
  from each snapshot's copy of the Podcasts DB. Designed for the case
  where the live DB lost history. Source-id dedup makes overlap with
  the live importer safe. Requires Full Disk Access and a mounted
  Time Machine drive.

## Spotify

- **One-shot historical (Extended Streaming History GDPR export)** —
  plugin `spotify-extended`, manual. Imports the lifetime archive
  Spotify produces when you request **Extended streaming history** at
  `spotify.com/account/privacy/`. Spotify emails the download link
  within ~30 days. Upload the zip; the importer reads every
  `Streaming_History_Audio_*.json` inside.
- **Live (via Last.fm scrobbling)** — set Spotify to scrobble to
  Last.fm, then run the `lastfm` plugin. Catches new listens hourly
  without waiting for a GDPR export.
- **One-shot historical (IFTTT/Pipedream backup zip)** — plugin
  `spotify-ifttt`. Built for the legacy IFTTT-to-Google-Drive backup
  pipeline. **Not registered as a default plugin** (kept in tree for
  manual backfill — see the source for the dated note);
  the code is still in `packages/media-helpers/fulcra_media/collect_plugins.py`
  for manual backfill via `uv run --package fulcra-media-helpers ...`.
- **[Not yet supported]** Live (Spotify Web API direct poll) — would need a
  Spotify OAuth flow we haven't built. Use Last.fm or the GDPR extended
  history in the meantime.

## Apple Music

- **Live (via Last.fm scrobbling)** — Apple Music can be configured to
  scrobble to Last.fm (via a third-party helper such as NepTunes or
  the Marvis Pro integration). Once it's scrobbling, the `lastfm`
  plugin captures it like any other source.
- **[Not yet supported]** Direct on-device read — there's no dedicated Apple
  Music plugin in the registry today.

## Deezer

- **Scheduled (Web API poll)** — plugin `deezer`, every 2 hours.
  Requires a Deezer OAuth access token with the `listening_history`
  permission. Mint one via Deezer's OAuth playground at
  `developers.deezer.com/api/oauth`, or run
  `fulcra-media wizard deezer` for a guided CLI flow. The token is
  stored in the macOS keychain.

## Last.fm

- **Scheduled (API poll)** — plugin `lastfm`, every hour. Requires a
  free Last.fm API key (create one at `last.fm/api/account/create` —
  callback URL and homepage can be left blank) plus your Last.fm
  **username**. Both are entered through the wizard; the API key goes
  in the keychain, the username in plugin settings.
- Last.fm also functions as the **universal music sidecar** —
  scrobbles from Spotify, Apple Music, Tidal, and most desktop/iOS
  players land in Last.fm, so this one plugin can cover services that
  have no direct integration.

## Trakt

- **Scheduled (OAuth API poll)** — plugin `trakt`, every 6 hours.
  Requires a free Trakt OAuth application (create at
  `trakt.tv/oauth/applications`). The wizard guides you through
  creating the app, pasting the Client ID + Client Secret, and signing
  in to grant access; tokens land in the keychain. Permissions
  `/checkin` and `/scrobble` should be **unchecked** in the Trakt app
  config — Fulcra Collect only reads history.
- Trakt is the **universal video sidecar** — its scrobbler plugins
  cover Netflix, Apple TV+, Plex, Jellyfin, and many other services,
  so the `trakt` plugin can substitute for several of the
  source-specific pathways below.
- Supports cluster-dedup and twin-dedup policies via plugin config
  (`clusters`, `twin_policy`, `cluster_threshold`) for users who have
  noisy backfilled history.

## Netflix

- **One-shot historical (CSV upload)** — plugin `netflix`, manual.
  Download `ViewingActivity.csv` from `netflix.com/Activity` (the
  **Download all** link at the bottom of the page), then upload it
  through the wizard. Click **Run now** to import; re-upload a fresh
  CSV to pick up newer watches.
- **Live (via Trakt)** — install Trakt's Netflix scrobbler and run
  the `trakt` plugin.

## Apple TV / Apple TV+

- **One-shot historical (Apple Data & Privacy takeout)** — plugin
  `apple-takeout`, manual. Request a copy of your data at
  `privacy.apple.com` and pick **Apple Media Services information**.
  Apple emails a download link within a few days. Unzip and upload
  `Playback Activity.csv` (or the folder containing it — the importer
  searches recursively). Each PLAY event becomes a Watched annotation.
- **Live (via Trakt)** — Apple TV+ can be scrobbled through Trakt's
  apps for iOS/tvOS. Run `trakt` to capture it.

## YouTube / YouTube Music

- **One-shot historical (Google Takeout)** — plugin `youtube`, manual.
  At `takeout.google.com` deselect everything except **YouTube and
  YouTube Music**, narrow to **history** only, pick **JSON** format,
  and submit. Google emails a download link (usually within a few
  hours). Unzip and upload `watch-history.json`. Re-run a Takeout when
  you want to refresh.
- **[Not yet supported]** Live YouTube watch capture — no continuous YouTube
  plugin exists today.

## Letterboxd

- **Scheduled (public RSS poll)** — plugin `letterboxd`, every 12
  hours. No API key — just your Letterboxd **username** (the slug
  after `letterboxd.com/` in your profile URL). Your diary must be
  public for the RSS feed to be reachable.

## Goodreads

- **Scheduled (public RSS poll)** — plugin `goodreads`, every 12
  hours. No API key — just your numeric Goodreads **user ID** (the
  number from `goodreads.com/user/show/<this>-your-name`). Profile
  must be public. Read-only; nothing is ever written back.

## Plex / Jellyfin

- **Live (webhook receiver)** — plugin `media-webhook`, runs as a
  service. The daemon binds a tiny HTTP server (default
  `127.0.0.1:8765`) that your media server POSTs playback events to.
  - **Plex:** **Settings -> Webhooks -> Add Webhook**,
    `http://127.0.0.1:8765/webhook`. **Requires Plex Pass.**
  - **Jellyfin:** **Dashboard -> Plugins -> Webhook -> Add Generic
    Destination** pointed at the same URL. Works on any tier.
  - Binding to a non-loopback host requires setting the
    `bearer-token` credential — the plugin refuses to start otherwise.

## Day One

- **Live (on-device DB read)** — plugin `dayone`, mode `live_app`,
  scheduled every 6 hours. Reads the running Day One app's SQLite
  database at
  `~/Library/Group Containers/*.dayoneapp2/Data/Documents/DayOne.sqlite`.
  Requires **Full Disk Access**. Caveat: don't fully quit Day One — your
  other-device entries only sync locally while the app is running.
- **One-shot historical (export ZIP/folder)** — plugin `dayone`, mode
  `export_file`. Use Day One's **File -> Export** to produce a JSON
  zip; point the plugin at the zip (or the unzipped folder) and click
  **Run now**.

## Browser activity (Fulcra Attention)

- **Live (browser extension)** — plugin `attention-relay` plus the
  Fulcra Attention browser extension. The extension watches your tabs
  (which are open, which is active, when you're idle) and POSTs events
  to the daemon's `POST /api/extension/attention` route directly. The
  plugin itself owns the Attention annotation definition and exposes a
  manual run that sanity-checks the pipeline (token present?
  definition bound? extension posted in the last 24h?). **Chromium
  browsers only** (Chrome / Edge / Brave / Arc / Vivaldi); Firefox and
  Safari are not supported yet. Build with `npm run build` in
  `packages/attention/chrome/` and load `dist/` as an unpacked
  extension; pair through the wizard.

## Generic RSS / Atom feed

- **Scheduled (RSS poll)** — plugin `generic-rss`, every 6 hours.
  Configure with a feed URL, a short **service** tag for labelling,
  and a **category** (`watched` / `listened` / `read`) that selects
  which canonical annotation to write to. Useful for sources that
  publish an RSS export (podcast feeds, bookmark services, blogs,
  status pages) but don't have a dedicated plugin.

## Generic media CSV

- **One-shot historical (CSV upload)** — plugin `generic-csv`, manual.
  Configure with the CSV path, a service tag, and a category. Optional
  column-map settings (`ts_col`, `title_col`, `subtitle_col`, `id_col`,
  `duration_col`, `end_col`) tell the importer how your CSV is shaped;
  defaults assume `timestamp` / `title` / `artist` / `id`, which matches
  common IFTTT/Pipedream exports. Useful for any tabular media history
  you can hand-shape — IFTTT dumps, hand-crafted spreadsheets, exports
  from less-common services.

---

**Last verified:** 2026-05-26 against the
`fulcra_collect.plugins` entry points in
`packages/{attention,dayone,media-helpers}/pyproject.toml` and the
plugin definitions in `collect_plugin.py` / `collect_plugins.py`. If
you add or remove a plugin, update this page.

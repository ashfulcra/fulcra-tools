# FulcraMediaHelpers

Import your media history — what you've **Watched**, **Listened to**, and **Read** — into your [Fulcra](https://fulcradynamics.com) personal data account, from ~13 sources, with per-row idempotency and agent-friendly JSON output.

```
$ fulcra-media import lastfm --json
{"importer":"lastfm","ok":true,"total":248,"skipped_existing":0,"posted":248,"verified":248,"since_watermark":"2026-05-16T22:00:00+00:00","new_watermark":"2026-05-17T08:42:00+00:00","would_post":null,"errors":[]}
```

## Why this exists

Your media history lives across a dozen services. Each has a different API (or no API at all), a different export format, and a different timestamp convention. Some give you everything (Spotify Extended, Netflix takeout). Some give you a 50-event rolling window (Spotify Web API). Some refuse to give you anything (Apple TV+, Hulu). Some are dead (Goodreads API).

This tool funnels all of them into Fulcra as `DurationAnnotation` events, so a single query over your Fulcra account answers questions like "what did I watch on weekends in 2024?" or "what was I listening to last March?" across **all** the services in one shot.

## Quickstart

```bash
pip install -e ".[dev]"
fulcra auth login          # via the fulcra-api CLI
fulcra-media bootstrap     # create the three annotation definitions
fulcra-media setup         # interactive picker — walks you through service onboarding
```

## What's supported

| Service | Command | How |
|---|---|---|
| **Last.fm** | `import lastfm` | API key + username (no OAuth). Covers Spotify, Apple Music, Tidal, Amazon Music, SoundCloud, Pandora, YouTube Music via in-app or Web Scrobbler. |
| **Spotify** | `import spotify-extended` | Spotify's official Extended Streaming History GDPR export. |
| **Spotify (legacy)** | `import spotify-ifttt` | Pre-Extended-API back-history from a legacy IFTTT → Google Drive applet. |
| **Deezer** | `import deezer` | Direct OAuth API. No per-call cap, cleaner than Spotify. |
| **Netflix** | `import netflix` | Slim (in-app) CSV or full GDPR export — auto-detected. |
| **Trakt** | `import trakt` | Direct API. Catches Apple TV+ via Universal Trakt Scrobbler. Cluster handling + cross-source twin dedup built in. |
| **Apple Podcasts** | `import apple-podcasts` | macOS local SQLite (`MTLibrary.sqlite`). Add `apple-podcasts-timemachine` for replay recovery from Time Machine backups. |
| **Apple TV / TV+ takeout** | `import apple-takeout` | privacy.apple.com → Apple Media Services → Playback Activity CSV. |
| **Letterboxd** | `import letterboxd` | Public RSS diary feed. |
| **Goodreads** | `import goodreads` | Public RSS of the 'read' shelf. |
| **YouTube** | `import youtube` | Google Takeout `watch-history.json` (recurring 2-month exports supported). |
| **Plex / Jellyfin** | `webhook` (long-running) | HTTP server that accepts `media.scrobble` / `PlaybackStop` events and ingests them in real time. |
| **Anything with a CSV** | `import generic-csv` | Column-mapped import. IFTTT, Pipedream, hand-rolled — any timestamp + title source. |
| **Anything with an RSS feed** | `import generic-rss` | Same for RSS/Atom feeds. |

Run `fulcra-media import --help` for the live list and `fulcra-media wizard <service>` for service-specific setup instructions.

## Categories

Events are split across three annotation definitions that `bootstrap` creates:

- **Watched** — TV, movies, video (Netflix, Trakt, Apple TV, Letterboxd, YouTube, Plex)
- **Listened** — music, podcasts (Last.fm, Spotify, Deezer, Apple Podcasts)
- **Read** — books (Goodreads)

Workouts are coming in a future revision against Fulcra's native workout data type; the `strava` importer module is in the repo but unwired from the CLI for now.

Each event carries a `content_fingerprint` for cross-source dedup (e.g. the same Dune episode imported from Netflix and Trakt collapses on a single fingerprint downstream).

## For agents

There's a skill at `skills/fulcra-media/SKILL.md` — load it when an AI agent needs to run imports on a user's behalf. Key contracts:

- Every `import` command supports `--json` (one-line envelope) and `--check-only` (dry-run)
- The envelope schema is stable & append-only: `{importer, ok, total, skipped_existing, posted, verified, since_watermark, new_watermark, would_post, errors[]}`
- Exit code 0 on `ok: true`, 2 on `ok: false`
- Errors carry a `stage` discriminator (`setup`, `auth`, `args`, `fetch`) so agents can pick a recovery action

Periodic-invocation cookbook:

```python
import json, subprocess
for importer in ("lastfm", "deezer", "trakt", "letterboxd", "goodreads"):
    res = subprocess.run(
        ["fulcra-media", "import", importer, "--json"],
        capture_output=True, text=True,
    )
    env = json.loads(res.stdout.strip()) if res.stdout.strip() else {"ok": False}
    if not env["ok"]:
        print(f"{importer}: failed at {env['errors'][0]['stage']}")
    elif env["posted"]:
        print(f"{importer}: +{env['posted']} (now at {env['new_watermark']})")
```

Watermarks (per-importer high-water marks) live in `~/.config/fulcra-media/state.json`; the next run picks up where the last one left off automatically.

## Architecture highlights

- **Watermark layer** (`fulcra_media/watermarks.py`) — API-poll importers fetch only what's new
- **Cross-batch twin cache** (`fulcra_media/twin_cache.py`) — high-confidence events from prior imports inform new-batch dedup of same-content-different-timestamp twins
- **Cluster preprocessing** (Trakt) — synthetic backfill timestamps get dropped, sentinel-dated, or kept per user choice
- **Source-id idempotency** — every event has a deterministic SHA-derived ID, so re-imports are silent no-ops at the Fulcra layer
- **JSON envelope** — agent-parseable output across every importer

The sibling project [fulcra-csv-importer](../fulcra-csv-importer) handles the general-purpose CSV→annotation parsing; this repo's `generic-csv` and `generic-rss` importers ride that library.

## Security

- All importer creds live under `~/.config/fulcra-media/` at mode 0600
- Exception strings containing auth-bearing URL params (`?access_token=…`, `?api_key=…`) are scrubbed before they land in `--json` error envelopes (via `cli_common.safe_exc_message`)
- `takeouts/` is gitignored; the repo's history was filter-repo'd to purge any personal CSVs that briefly slipped through
- Probe scripts in `scripts/` require `--i-know-this-hits-prod` to run

## Development

```bash
uv sync --extra dev
uv run pytest                  # 415 tests as of this writing
uv run fulcra-media --help
```

Architecture docs:
- `docs/superpowers/specs/2026-05-16-fulcra-media-helpers-design.md` — design
- `docs/superpowers/research/2026-05-17-media-service-pathways.md` — landscape of media services
- `docs/superpowers/plans/` — feature plans

## Status

- 415 unit tests passing
- All importers exercised against real or fixture-shaped data
- Schema covers three categories (workouts pending native-type rework)
- Webhook receiver tested against simulated Plex/Jellyfin payloads; needs a Plex Pass user for real-data validation
- Goodreads / Letterboxd: RSS endpoint quirks documented in their wizards

## License

Personal-use project. No license declared yet.

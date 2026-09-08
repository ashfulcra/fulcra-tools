# FulcraMediaHelpers

Import what you've watched, listened to, and read into your
[Fulcra](https://fulcradynamics.com) account. This package supplies Collect's
media plugins and a standalone command-line tool.

It is part of the [unsupported, vibe-coded monorepo](../../README.md). The
sources have different ideas about what a timestamp means. This package tries
to preserve those differences instead of turning a tidy timeline into a false one.

## Use with Collect

The [Mac installer](../../docs/collect.md#get-started-new-user) includes these
plugins. Open the dashboard, choose a source, and follow **Set up**. You may
need a service login, API key, local app permission, or an export file.
[Available plugins and release status](../../docs/collect.md#plugin-status).

The [source guide](../../docs/how-do-i-get-my-data.md) covers each pathway.
Spotify uses an extended-history export or Last.fm scrobbles; there is no direct
live Spotify plugin. The legacy `spotify-ifttt` importer is CLI-only.

The commands below are for source installations and agent workflows. Example
output uses synthetic data.

```
$ fulcra-media import lastfm --json
{"importer":"lastfm","ok":true,"total":3,"skipped_existing":0,"posted":3,"verified":3,"since_watermark":"2026-01-01T00:00:00+00:00","new_watermark":"2026-01-02T00:00:00+00:00","would_post":null,"errors":[]}
```

## Why this exists

Your media history lives across services with different APIs, export formats,
and timestamp conventions. A Spotify extended-history export, a Netflix
date-only row, and a local Apple TV cache are very different kinds of evidence.

The importers normalize supported sources into Fulcra `DurationAnnotation`
events under Watched, Listened, and Read. That lets an agent query the history
you actually imported across sources, including the provenance and timestamp
limitations, instead of sending you back through each service's export screen.

## CLI quickstart

Install the [workspace environment](../collect/README.md#running-from-source),
then run these commands from the repository root. The CLI does not need the
Collect daemon running. Login and bootstrap contact Fulcra; bootstrap creates
the destination definitions.

```bash
uv run --package fulcra-media-helpers --extra fulcra-cli fulcra auth login
uv run --package fulcra-media-helpers fulcra-media bootstrap
uv run --package fulcra-media-helpers fulcra-media setup  # interactive; needs a terminal
```

## What's supported

| Service | Command | How |
|---|---|---|
| **Last.fm** | `import lastfm` | API key + username (no OAuth). Covers Spotify, Apple Music, Tidal, Amazon Music, SoundCloud, Pandora, YouTube Music via in-app or Web Scrobbler. |
| **Spotify** | `import spotify-extended` | Spotify's official Extended Streaming History GDPR export. |
| **Spotify (legacy)** | `import spotify-ifttt` | Pre-Extended-API back-history from a legacy IFTTT → Google Drive applet. |
| **Deezer** | `import deezer` | History API with an OAuth access token; requires working provider credentials. |
| **Netflix** | `import netflix` | Slim (in-app) CSV or full GDPR export — auto-detected. |
| **Trakt** | `import trakt` | Direct API. Catches Apple TV+ via Universal Trakt Scrobbler. Cluster handling + cross-source twin dedup built in. |
| **Apple Podcasts** | `import apple-podcasts` | macOS local SQLite (`MTLibrary.sqlite`). Add `apple-podcasts-timemachine` for replay recovery from Time Machine backups. |
| **Apple Music takeout** | Collect plugin `apple-music-takeout` | Apple Data & Privacy play-activity CSV. |
| **Apple TV on-device** | Collect plugin `apple-tv` | Local Watch Now cache; requires Full Disk Access and a recently opened TV app. |
| **Apple TV / TV+ takeout** | `import apple-takeout` | privacy.apple.com → Apple Media Services → Playback Activity CSV. |
| **Letterboxd** | `import letterboxd` | Public RSS diary feed. |
| **Goodreads** | `import goodreads` | Public RSS of the 'read' shelf. |
| **YouTube** | `import youtube` | Google Takeout `watch-history.json` (recurring 2-month exports supported). |
| **Plex / Jellyfin** | `webhook` (long-running) | HTTP server that accepts `media.scrobble` / `PlaybackStop` events and ingests them in real time. |
| **Anything with a CSV** | `import generic-csv` | Column-mapped import. IFTTT, Pipedream, hand-rolled — any timestamp + title source. |
| **Anything with an RSS feed** | `import generic-rss` | Same for RSS/Atom feeds. |

Apple Music takeout and on-device Apple TV are Collect plugins; this CLI has no
matching `import` command for them.

Run `fulcra-media import --help` for the live CLI list and `fulcra-media wizard <service>` for service-specific setup instructions.

## Categories

Events are split across three annotation definitions that `bootstrap` creates:

- **Watched** — TV, movies, video (Netflix, Trakt, Apple TV, Letterboxd, YouTube, Plex)
- **Listened** — music, podcasts (Last.fm, Spotify, Deezer, Apple Podcasts)
- **Read** — books (Goodreads)

Workouts are coming in a future revision against Fulcra's native workout data type; the `strava` importer module is in the repo but unwired from the CLI for now.

Where available, events carry content fingerprints for cross-source dedup.
The import pipeline compares these and deterministic source IDs against
readback; Trakt also offers a policy for low-confidence twins. Matching depends
on source metadata, so a shared title alone is not a promise that two events
will collapse.

## For agents

The [fulcra-media skill](skills/fulcra-media/SKILL.md), linked from this
package's [AGENTS.md](AGENTS.md), describes the shell contract and importer
selection. It can be used by an agent runtime that can invoke `fulcra-media`.
For recurring work, use Collect's scheduler or your host's scheduler; installing
a skill alone does not schedule anything. Letta's intra-turn heartbeat flag is
not a recurring scheduler.

Key contracts:

- Every `import` command supports `--json` (one-line envelope) and `--check-only` (dry-run)
- The envelope schema is stable & append-only: `{importer, ok, total, skipped_existing, posted, verified, since_watermark, new_watermark, would_post, errors[]}`
- Exit code 0 on `ok: true`, 2 on `ok: false`
- Errors carry a `stage` discriminator (`setup`, `auth`, `args`, `fetch`, `snapshot`) so agents can pick a recovery action

`--check-only` skips ingest, but still reads the source and Fulcra readback;
it is not an offline command. The long-running `webhook --json` command has a
separate lifecycle stream: `ready` is emitted after `/health` has served a
request, and `shutdown` reports final counters.

Periodic-invocation cookbook:

```python
import json, subprocess
for importer in ("lastfm", "deezer", "trakt", "letterboxd", "goodreads"):
    res = subprocess.run(
        ["fulcra-media", "import", importer, "--json"],
        capture_output=True, text=True,
    )
    if not res.stdout.strip():
        raise RuntimeError(f"{importer}: no JSON result (exit {res.returncode})")
    env = json.loads(res.stdout)
    if res.returncode != 0 or not env.get("ok"):
        errors = env.get("errors") or [{"stage": "unknown"}]
        raise RuntimeError(f"{importer}: failed at {errors[0].get('stage', 'unknown')}")
    if env["posted"]:
        print(f"{importer}: +{env['posted']} (now at {env['new_watermark']})")
```

CLI watermarks live in `~/.config/fulcra-media/state.json` (override with
`FULCRA_MEDIA_STATE`). Last.fm, Deezer, Letterboxd, Goodreads, and generic RSS
advance timestamp watermarks; Goodreads and generic RSS include the user or
feed in their key. Export imports, Trakt, and Apple Podcasts use readback/dedup
without advancing those watermarks. Collect supplies its own state and dedup
claims through the plugin context.

## Architecture highlights

- **Watermark layer** (`fulcra_media/watermarks.py`) — API-poll importers fetch only what's new
- **Cross-batch twin cache** (`fulcra_media/twin_cache.py`) — high-confidence events from prior imports inform new-batch dedup of same-content-different-timestamp twins
- **Cluster preprocessing** (Trakt) — synthetic backfill timestamps get dropped, sentinel-dated, or kept per user choice (default: **keep**)
- **Timestamp provenance** — low-confidence Trakt clusters carry
  `[bulk import — time unreliable]`; other low-confidence events carry
  `[time unreliable]`. Netflix date-only rows use synthetic noon, while Apple
  TV Recently Watched rows use a fetch-time upper bound. These labels explain
  uncertainty; they cannot recover the actual viewing time. The note carries
  this distinction because the typed wire format drops `timestamp_confidence`.
- **Dedup and verification** — deterministic source IDs plus readback, with
  daemon-backed claims when run through Collect. The typed ingest endpoint has
  no server-side source-ID dedup. Delayed readback checks whether posted records
  became visible; a failed or incomplete verification is not proof of delivery.
- **JSON envelope** — agent-parseable output across every importer

The sibling [fulcra-csv-importer](../csv-importer/README.md) supplies shared
parsing and dedup helpers used by the generic importers.

## Security

- Standalone CLI credential files live under `~/.config/fulcra-media/` at mode
  0600; Collect's configured plugin credentials use its OS keychain instead.
- Exception strings containing auth-bearing URL params (`?access_token=…`, `?api_key=…`) are scrubbed before they land in `--json` error envelopes (via `cli_common.safe_exc_message`)
- Keep takeouts and other personal exports out of version control; follow the
  repository's [privacy rules](../../docs/PRIVACY.md).
- Probe scripts in `scripts/` require `--i-know-this-hits-prod` to run

## Development

```bash
# From the repository root:
uv run --package fulcra-media-helpers --extra dev pytest packages/media-helpers/tests/ -q
uv run --package fulcra-media-helpers fulcra-media --help
```

## Status

- The test suite covers synthetic importer data, dedup, wizard contracts, and
  simulated webhook payloads; it does not establish current access to every provider.
- Schema covers three categories (workouts pending native-type rework)
- Plex/Jellyfin account setup and real-device delivery require separate validation.
- Goodreads / Letterboxd: RSS endpoint quirks documented in their wizards

## License

Personal-use experiment; no package license is declared in [pyproject.toml](pyproject.toml).

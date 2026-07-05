---
name: fulcra-media
description: Import media history (Watched / Listened / Read) into a Fulcra account via the `fulcra-media` CLI. Use when the user wants to track Netflix, Trakt, Spotify, Last.fm, Apple Podcasts, Letterboxd, Goodreads, YouTube, Plex/Jellyfin, or any other media source listed in `fulcra-media import --help`.
---

# fulcra-media — Import a user's media history into Fulcra

`fulcra-media` is a Python CLI that imports media events (watches, listens, books read) into Fulcra as DurationAnnotations. Each import is idempotent (re-runs are safe), and every command supports a `--json` flag for machine-readable output and a `--check-only` dry-run.

This skill helps you (the AI agent) run imports on the user's behalf, schedule periodic syncs, and interpret the results.

**Runtime-agnostic.** Everything below is shell I/O. This skill works the same in Claude Code, OpenCode, Codex, Gemini CLI, Copilot CLI, or any agent runtime that can execute a subprocess. No Claude-Code-specific tools are required — the only contract is `fulcra-media` on PATH and the JSON envelope schema.

---

## Where to start — the re-entrancy probes

Before running an import, probe how far this user already got. The states are a prefix of the flow —
**authed? → defs bootstrapped? → anything to import? → already synced?** — so enter at the **first
probe that fails** (per the repo's skill-quality pattern, `docs/skill-quality-pattern.md`). Every
state is safely re-enterable: `bootstrap` is idempotent, and each import is idempotent (deterministic
source_ids + the watermark layer), so re-probing or re-importing never double-counts. The rows below
consolidate the probe *concepts* used later in this skill (the `--check-only`/`would_post` probe in
"Is there anything new to post?" and the heartbeat contract's "Inspect first / Probe before paying"
steps) into one ordered entry point — see those sections for the fuller treatment.

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Authed? | `fulcra auth print-access-token` | exits 0 and prints a non-empty token (the CLI mints/refreshes it; `FULCRA_ACCESS_TOKEN` in the env also satisfies this) | **AUTH** — tell the user to run `fulcra auth login` (interactive browser flow); see "Fulcra Life API auth" below |
| Defs bootstrapped? | `fulcra-media status` | the JSON's `watched_definition_id` / `listened_definition_id` / `read_definition_id` are non-null for the category you're importing (Watched for netflix/trakt/youtube/…, Listened for lastfm/deezer/spotify/…, Read for goodreads) | **BOOTSTRAP** — run `fulcra-media bootstrap` (idempotent; no-op once defs exist) |
| Anything to import? | `fulcra-media import <name> --check-only --json` then read `would_post` | `would_post` > 0 (`--check-only` skips the POST; costs an API readback but no ingest) | **IMPORT** — run `fulcra-media import <name> --json` for real; `would_post: 0` means nothing new, so skip |
| Already synced? | `fulcra-media status` and read `watermarks["<name>"]` | an ISO timestamp is present and recent for this importer (empty/absent = cold-start; a stale one just means it's due for another run) | **IMPORT** — cold-start or stale; run the real import to advance the watermark |

All probes pass with `would_post: 0` and a fresh watermark → this importer is already synced; move on
to the next source or tell the user they're up to date. A brand-new user fails the first probe.
The `fulcra …` (not `fulcra-media …`) auth probe belongs to the separate
[fulcra-api](https://github.com/fulcradynamics/fulcra-api-python) CLI; `fulcra-media` has no auth
subcommand of its own.

---

## Quick orientation — what's already set up?

Always start with:

```bash
fulcra-media status
```

The output (JSON) tells you:
- Which annotation definitions exist (`watched_definition_id`, `listened_definition_id`, `read_definition_id`) — if `null`, run `fulcra-media bootstrap` to create them
- Per-importer watermarks (`watermarks["lastfm"]`, `watermarks["trakt"]`, ...) — empty means cold-start, ISO timestamp means incremental
- Service-tag UUIDs the user has already created

If the user is brand new, walk them through `fulcra-media setup` (interactive picker) — but **don't run setup non-interactively**, it expects stdin input from a TTY.

---

## The import envelope — the only output you need to parse

Every `fulcra-media import <importer> --json` writes exactly one line of JSON to stdout. Schema is **stable and append-only**:

```json
{
  "importer": "lastfm",
  "ok": true,
  "total": 248,
  "skipped_existing": 0,
  "posted": 248,
  "verified": 248,
  "since_watermark": "2026-05-16T22:00:00+00:00",
  "new_watermark": "2026-05-17T08:42:00+00:00",
  "would_post": null,
  "errors": []
}
```

- `ok: true` + `exit code 0` → success
- `ok: false` + `exit code 2` → failure. Inspect `errors[].stage` (one of `setup`, `auth`, `args`, `fetch`):
  - `setup` → run `fulcra-media bootstrap` first
  - `auth` → creds file missing or invalid; direct the user to the wizard
  - `args` → flag parse error (bad timezone, missing path)
  - `fetch` → upstream API failed; the `message` is scrubbed of credential params

`--check-only` adds a non-null `would_post` integer (how many events *would* post) and skips the actual POST. Use this for cheap "is there anything to import?" probes.

`since_watermark` / `new_watermark` describe the incremental cursor. On the first run both are `null`. On subsequent runs `since_watermark` reflects what was passed (watermark - overlap_hours for services with reorder hazard); `new_watermark` is the high-water mark from the just-completed run, written back to state.

**Never parse stdout in human mode** — only the `--json` envelope is a stable contract.

---

## Importer roster (what works today)

Run `fulcra-media import --help` for the live list. As of this skill version:

| Command | Category | Auth | Notes |
|---|---|---|---|
| `lastfm` | listened | API key + username | **Universal music sidecar** — covers Tidal/Apple Music/Amazon Music/SoundCloud/Pandora/YouTube Music via in-app or Web Scrobbler. Public scrobbles only. |
| `deezer` | listened | OAuth access token | Direct API, no per-call cap. Cleaner than Spotify. Manual token mint. |
| `spotify-extended` | listened | GDPR zip | Full history; one-shot, slow to arrive. |
| `spotify-ifttt` | listened | Legacy GDrive zip | Pre-Extended-API backfill; cross-applet dedup native. |
| `trakt` | watched | OAuth | Covers Apple TV+ via Universal Trakt Scrobbler. Cluster handling: `--clusters drop|sentinel:YYYY|keep|ask`. Twin-dedup: `--twin-policy ask|auto-discard|keep`. |
| `netflix` | watched | GDPR CSV | Slim (in-app) or rich (full GDPR) — auto-detected. |
| `apple-podcasts` | listened | Local SQLite | macOS only. Use `apple-podcasts-timemachine` for snapshot recovery. |
| `apple-takeout` | watched | privacy.apple.com CSV | Apple TV Playback Activity. |
| `letterboxd` | watched | Username | Public RSS. |
| `goodreads` | read | User ID | Public RSS of the 'read' shelf. |
| `youtube` | watched | Google Takeout zip | watch-history.json. Recurring exports every 2 months. |
| `generic-csv` | watched/listened | n/a | Any column-mapped CSV. |
| `generic-rss` | watched/listened | n/a | Any RSS/Atom feed. |
| (server) `webhook` | watched | bearer token | Long-running HTTP receiver for Plex/Jellyfin push. Not an `import` subcommand. |

---

## Recipes

### Daily cron / agent-driven incremental sync

Per-importer pattern:

```bash
RES=$(fulcra-media import lastfm --json)
echo "$RES" | jq -r '
  if .ok then "lastfm: +\(.posted) (watermark→\(.new_watermark // "n/a"))"
  else "lastfm FAILED: \(.errors[0].stage): \(.errors[0].message)" end
'
```

For multi-service runs, iterate over the user's configured importers (anything in `state.watermarks` is fair game):

```bash
fulcra-media status | jq -r '.watermarks | keys[]' | while read importer; do
  fulcra-media import "$importer" --json
done
```

The watermark layer makes each run cheap when nothing's new (`skipped_existing` will trend up, `posted` will be 0).

### "Is there anything new to post?" — without paying ingest cost

```bash
fulcra-media import lastfm --check-only --json | jq '.would_post'
# > 14 means 14 events would land; 0 means nothing new
```

Useful for high-frequency polling (every minute) without ingest churn — only run the real import when `would_post > 0`.

### Bootstrap a fresh user

```bash
fulcra-media bootstrap  # creates Watched, Listened, Read defs
```

Idempotent — subsequent calls are no-ops once defs exist.

### Reset everything (rare)

```bash
fulcra-media reset --confirm  # soft-deletes all three defs, clears watermarks + twin cache
```

⚠️ Fulcra has **no per-event delete**. Events under reset-soft-deleted defs stay visible in queries forever (Fulcra limitation). The next `bootstrap` creates fresh defs with new UUIDs, so future imports namespace cleanly. Use this only after a meaningful pipeline change you want to re-run against (e.g. a cluster-policy change).

---

## Picking an importer — decision tree

User says they want to track X →

| User mentions | Run |
|---|---|
| Spotify (recent) | `lastfm` (if scrobbling) else `spotify-extended` (one-shot full history) |
| Apple Music / Tidal / SoundCloud / Pandora / YouTube Music / Amazon Music | `lastfm` (with Web Scrobbler browser extension if needed) |
| Netflix | `netflix` (slim CSV from privacy settings, or full GDPR export) |
| Hulu / Disney+ / Max / Prime Video / Peacock | No direct path — use `trakt` for ongoing, GDPR-export-and-`generic-csv` for backfill |
| Apple TV+ | `trakt` (with Universal Trakt Scrobbler browser ext) for ongoing; `apple-takeout` for backfill |
| YouTube (videos) | `youtube` against the Takeout watch-history.json (recurring 2-month exports) |
| Apple Podcasts | `apple-podcasts` (macOS only); add `apple-podcasts-timemachine` for replay recovery |
| Letterboxd | `letterboxd --username <user>` |
| Goodreads | `goodreads --user-id <id>` |
| Workouts (Strava etc.) | Not currently wired — workouts will land in Fulcra's native workout data type in a future revision. The `strava` importer module is still in the repo (frozen for rewrite); don't suggest it yet. |
| Plex | `webhook` (run the receiver, point Plex webhooks at it) |
| Jellyfin | `webhook` (run the receiver, point jellyfin-plugin-webhook at it) |
| **Anything else with a CSV they got somewhere** | `generic-csv` with column flags |
| **Anything else with an RSS feed** | `generic-rss` |
| Pocket | Service is dead (shut down 2025-07). Redirect to whatever the user migrated to (Readwise Reader / Instapaper / Raindrop.io). |

---

## Wizards — when to invoke them

`fulcra-media wizard <name>` prints onboarding instructions for the given service. **The output is plain text for the user, not for you.** Don't try to parse it. If the user asks "how do I set up X," just call `fulcra-media wizard X` and show them the output.

Available wizards: `netflix trakt apple-podcasts spotify spotify-ifttt apple-takeout ifttt pipedream lastfm deezer letterboxd goodreads youtube plex jellyfin`.

The interactive `fulcra-media setup` walks the user through category-picking and then drops them into the right wizard. **Only invoke setup from a TTY** — it expects keyboard input.

---

## Credential file locations

All creds live under `~/.config/fulcra-media/` with mode 0600:

| File | For | Shape |
|---|---|---|
| `state.json` | All importers | `{watched_definition_id, listened_definition_id, read_definition_id, tag_ids, watermarks}` |
| `lastfm.json` | Last.fm | `{username, api_key}` |
| `deezer.json` | Deezer | `{access_token}` |
| `trakt.json` | Trakt | OAuth blob managed by the importer |
| `twin_cache.json` | All | `{<content_fingerprint>: {source_id, importer, start_time, confidence}}` for cross-batch dedup |

Don't echo these files' contents to stdout — especially in `--json` mode where logs may be captured. The CLI scrubs auth-bearing URL params from exception messages via `safe_exc_message`, but the raw creds files are sensitive.

---

## Periodic invocation for agentic workers

Recommended cadence:

| Importer | Cadence |
|---|---|
| `lastfm`, `deezer`, `trakt` | hourly (API + watermark) |
| `letterboxd`, `goodreads`, `generic-rss` | daily (RSS feeds change slowly) |
| `apple-podcasts` | hourly (cheap — local SQLite snapshot) |
| `netflix`, `spotify-extended`, `apple-takeout`, `spotify-ifttt`, `youtube` | on-demand (user-uploaded zips / recurring Takeouts) |
| `webhook` (Plex/Jellyfin) | persistent service (`launchd` / `systemd`) |

A simple agent driver:

```python
import json, subprocess
for importer in ("lastfm", "deezer", "trakt"):
    res = subprocess.run(
        ["fulcra-media", "import", importer, "--json"],
        capture_output=True, text=True,
    )
    env = json.loads(res.stdout) if res.stdout.strip() else {"ok": False}
    if not env.get("ok"):
        notify_user(f"{importer} failed at {env.get('errors', [{}])[0].get('stage')}")
    elif env.get("posted"):
        log(f"{importer}: +{env['posted']} (now at {env['new_watermark']})")
```

The `webhook` receiver is a long-running process — don't poll it; start it once and supervise it (`launchctl`, `systemd`, `pm2`, whatever).

---

## Running on a heartbeat (Hermes, openclaw, OpenHands, and friends)

Several agent runtimes natively support recurring/scheduled wake-ups:

| Runtime | Mechanism | Notes |
|---|---|---|
| **Hermes Agent** (NousResearch) | First-class `heartbeat` primitive — recurring wake-ups that fire a bounded micro-action and report back. Also has built-in cron. | The phrase "heartbeat" in NousResearch's sense means **scheduled wake-up** with fresh agent context. |
| **openclaw** | First-class **Cron jobs** tool (alongside bash/process/read/write/edit). Skills live at `~/.openclaw/workspace/skills/<skill>/SKILL.md`. | Drop this SKILL.md into that path verbatim. |
| **OpenHands** (formerly OpenDevin) | Sandboxed automations — scheduled tasks. Loader auto-picks up `SKILL.md`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`. | Bash sandboxing is built-in. |
| **Claude Code / Aider / Cursor / Continue.dev** | No native heartbeat — use host `cron` / `launchd` / `systemd-timer`. | These read `AGENTS.md` / `CLAUDE.md` from the repo root. A copy of this skill lives at `AGENTS.md` for that reason. |
| **Letta / MemGPT** | **Different meaning** — `request_heartbeat: bool` is an intra-turn flag that keeps the tool loop going within a single user turn, not a scheduler. Still needs an external scheduler to run `fulcra-media` periodically. | Don't conflate. |
| **SmolAgents / AutoGen / CrewAI / LangGraph** | Function-calling orchestration; no native scheduler. | Run from host cron, or wrap calls in a single `execute_code` tool. |

### The recurring-task contract

When running this skill on a heartbeat (any runtime), the action you fire each tick should be **bounded, idempotent, and stateful** in this exact shape:

1. **Inspect first.** `fulcra-media status` (cheap, local, no API) tells you which importers have watermarks and how stale each one is. Skip importers whose `watermarks[<name>]` is fresher than their recommended cadence.
2. **Probe before paying.** For each candidate importer, fire `fulcra-media import <name> --check-only --json` and read `would_post`. If 0, skip. This costs an API readback but no ingest.
3. **Import when warranted.** `fulcra-media import <name> --json`. Parse the envelope. On `ok: false`, surface `errors[0].stage` to the user via whatever channel the runtime provides — don't retry mechanically from inside the heartbeat tick.
4. **Cap per-tick wallclock.** A single heartbeat should not import every source on every tick. Round-robin or pick whichever importer is most stale; ingestion of a large historical zip (`spotify-extended`, `netflix` full GDPR) is a one-shot, not a heartbeat job — surface those as "user-uploaded" and run them ad-hoc.
5. **Lock if your runtime can re-enter.** If the runtime can fire two heartbeats concurrently (it generally shouldn't), put a flock around the import:

   ```bash
   flock -n /tmp/fulcra-media.lock fulcra-media import lastfm --json
   ```

6. **Report status to wherever your runtime expects it.** Hermes has a default "consolidate memory / write status file" maintenance heartbeat — write the last-tick envelope summary there. openclaw cron jobs can pipe to its sessions log. For host cron, route stdout/stderr to a logfile the next tick can read.

### A heartbeat-tick template

The CLI already manages its own per-importer watermarks, so a heartbeat tick can be small:

```bash
#!/usr/bin/env bash
# Single heartbeat tick. Idempotent; safe to fire as often as your runtime allows.
# The CLI's watermark layer guarantees no double-imports — re-runs are cheap.
set -euo pipefail
LOG=/var/log/fulcra-media-heartbeat.log

for importer in lastfm deezer trakt apple-podcasts letterboxd goodreads; do
  # Cheap probe — no ingest cost, just an API readback.
  pending=$(fulcra-media import "$importer" --check-only --json | jq '.would_post')
  [ "$pending" = "0" ] && continue
  fulcra-media import "$importer" --json >> "$LOG" 2>&1
done
```

Drop that in a `cron job` (openclaw), `heartbeat` (Hermes), `automation` (OpenHands), or host `cron`/`launchd` — same script, same result. Portable across BSD (macOS) and GNU (Linux) because it leans on the CLI for all timestamp arithmetic.

---

## Common failure patterns

### `ok: false`, `errors[0].stage = "auth"`

Creds file missing or expired. Tell the user to run `fulcra-media wizard <importer>`. For OAuth-based importers (Trakt, Deezer), the wizard explains the manual token mint flow.

### `ok: true`, `verified: 0`, `posted > 0`

Fulcra's ingest-to-query indexing lag (seconds to minutes for bulk imports). This is **not a failure** — events are accepted, just not yet visible in readback. The next `--check-only` will report 0 to post.

### `posted > 0` but events don't appear in `fulcra get-records DurationAnnotation`

Three possible causes:
1. Indexing lag (wait 1-5 min)
2. You're querying a different annotation def — check `fulcra-media status` to confirm the active def UUID matches what your query expects
3. Source-id collision with an older soft-deleted def — events still exist but their source_id points at a deleted def. Either filter client-side or `fulcra-media reset --confirm` + `bootstrap` + re-import.

### Trakt import shows thousands of events on the same 1-2 days

Trakt stamps historical backfill with the user's signup date when the source service didn't provide a real timestamp. The importer detects clusters (default `--cluster-threshold 5`) and offers handling: `--clusters drop` (discard), `--clusters sentinel:2015` (shift to Jan 1 2015 so they don't pollute current data), `--clusters keep` (leave at signup-day, low confidence). On a TTY the default is interactive `ask`.

---

## Fulcra Life API auth

The CLI shells out to `fulcra auth print-access-token` (from the [fulcra-api](https://github.com/fulcradynamics/fulcra-api-python) package) to mint tokens. If a user has never logged in, that command will fail with "credentials not found" — tell them to run `fulcra auth login` (interactive browser flow).

Token override: `FULCRA_ACCESS_TOKEN=...` skips the shell-out. Useful for non-interactive agents that have a long-lived token via another route.

---

## Architectural references (for the curious agent)

- `~/Developer/FulcraMediaHelpers/docs/superpowers/research/2026-05-17-media-service-pathways.md` — full landscape of media services and the recommended pathway per service
- `~/Developer/FulcraMediaHelpers/docs/superpowers/specs/2026-05-16-fulcra-media-helpers-design.md` — original design doc
- `~/Developer/FulcraMediaHelpers/docs/superpowers/plans/2026-05-17-lastfm-and-periodic-architecture.md` — the periodic-update + watermark architecture
- Sibling project `fulcra-csv-importer` (`/<repo>/skills/fulcra-csv/SKILL.md`) — general CSV → any Fulcra annotation, used internally by `generic-csv`

---

## Don't

- **Don't run `setup` non-interactively** — it expects TTY input
- **Don't re-implement watermark logic in the agent** — let the CLI manage `state.json`
- **Don't parse human-mode stdout** — only `--json` is a stable contract
- **Don't print creds files to stdout** — they're 0600 for a reason
- **Don't soft-delete the user's annotation defs without explicit confirmation** — events under deleted defs stay visible (Fulcra has no per-event delete)
- **Don't assume an importer maps to a single category** — `generic-csv` and `generic-rss` are caller-specified; check `--help`

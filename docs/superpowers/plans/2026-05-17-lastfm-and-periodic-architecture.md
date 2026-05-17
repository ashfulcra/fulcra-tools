# Last.fm importer + periodic-update architecture

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two intertwined deliverables:
1. A Last.fm scrobble importer with incremental capture suitable for ongoing use.
2. Generalize the run-import architecture so any API-poll importer (Last.fm, Trakt, future YouTube, future Spotify-API) can be safely executed on a schedule by an AI agent (openclaw-style), with watermarks, JSON output, and `--check-only` dry-runs.

**Architecture:** A new `Watermark` abstraction lives in `state.py`; every API-poll importer registers a name and stores its high-water mark there. The CLI gains a `--json` flag (machine-readable output for agents) and a `--check-only` flag (counts new items without posting). Last.fm uses `user.getRecentTracks?from=<watermark>` for incremental fetch; falls back to a full backfill on first run.

**Tech Stack:** httpx (existing), click (existing), Last.fm Audioscrobbler 2.0 REST API (read-only API-key auth), state.json (existing watermark slot, finally used).

---

## 1. Service landscape (pathway recommendations)

Full landscape research lives in [docs/superpowers/research/2026-05-17-media-service-pathways.md](../research/2026-05-17-media-service-pathways.md). Key takeaways that shape this plan:

- **Last.fm is the universal music sidecar.** Tidal/Amazon Music/SoundCloud/Pandora/YouTube Music all funnel into Last.fm via Web Scrobbler or native settings. The Last.fm importer ends up being the single most-leveraged piece of music infrastructure after Spotify.
- **Onboarding is just username + API key.** Read-only `user.get*` methods don't need OAuth. No callback URL, no client secret. Far friendlier than every other "real" API researched.
- **RSS deserves a generic importer.** Letterboxd and Goodreads — closed/dying APIs — both publish stable RSS feeds. A new `generic-rss` importer would pair well with the existing `generic-csv`. Out of scope for this plan; flagged as a sibling.
- **Trakt + Universal Trakt Scrobbler is the catch-all for streaming video.** Apple TV+, plus anything UTS scrobbles, lands in Trakt → the existing importer.
- **Plex / Jellyfin want webhook receivers, not pollers.** Their best capture pathway is push, not pull. This is the AI-agent integration pattern the prompt called out and is best implemented as a separate "webhook receiver" mode — out of scope for this plan, but the watermark/agent architecture below assumes that mode exists in the future.
- **Pocket is dead** (shut down 2025-07-08; export window closed 2025-11-12). Users who mention it need to be redirected to the replacement they migrated to (Readwise Reader has the best API; Instapaper + Raindrop.io are also viable).
- **Deezer is a surprise winner.** Real OAuth `user.history` endpoint with no per-call cap — cleaner than Spotify's recently-played. Worth a dedicated importer once Spotify is solid.
- **IFTTT is mostly dead for this domain.** Only Spotify and Strava are practically usable on IFTTT now. Pipedream is preferred where it has a real OAuth app.

The 7 pathways in priority order (read research doc for the full per-service grid):
1. **Direct API** — OAuth + incremental endpoint
2. **Webhook receiver** — service POSTs to us (Plex/Jellyfin/Strava)
3. **GDPR export** — one-shot history download
4. **Pipedream scheduled workflow** — OAuth token mgmt + 1-min cron → CSV
5. **IFTTT applet** — 5-15 min polling → Google Sheets
6. **Local DB/snapshot** — desktop app SQLite/plist scrape
7. **Browser extension / RSS** — third-party scraper or feed
8. **No viable path** — flag for user, suggest manual logging via `generic-csv`

Service catalog rows (data-driven for the `setup` wizard in Task 7):

| Service | Tier | Ongoing pathway | Importer | Wizard |
|---|---|---|---|---|
| Netflix | 1 | CSV / GDPR | `netflix` | `netflix` |
| Spotify | 1 | GDPR + (future) Pipedream poll | `spotify-extended` | `spotify` |
| Spotify (IFTTT legacy) | 1 | Legacy import | `spotify-ifttt` | `spotify-ifttt` |
| Trakt | 1 | Direct API | `trakt` | `trakt` |
| Apple Podcasts | 1 | Local SQLite | `apple-podcasts` | `apple-podcasts` |
| Apple TV+ | 1 | Trakt via UTS | (use `trakt`) | (extends trakt wizard) |
| **Last.fm** | **1** | **Direct API (this plan)** | **`lastfm`** | **`lastfm`** |
| Strava | 2 | Direct API + webhook | (future `strava`) | (future) |
| Deezer | 2 | Direct API | (future `deezer`) | (future) |
| Plex / Jellyfin | 2 | Webhook receiver | (future receiver mode) | (future) |
| Letterboxd | 2 | RSS | (future `generic-rss`) | (future) |
| Goodreads | 2 | RSS | (future `generic-rss`) | (future) |
| YouTube | 3 | Google Takeout (scheduled) | `apple-takeout`-style new importer | (future) |
| Apple Music | 3 | API (high friction) or GDPR | (future) | (future) |
| Tidal/Amazon Music/SoundCloud/Pandora/YouTube Music | 3 | Sidecar via Last.fm | `lastfm` | (mention in lastfm wizard) |
| Audible/Kindle | 3 | Unofficial cookie API | (future) | (future) |
| Bandcamp | 3 | Collection-page scrape (purchases, not plays) | (future) | (future) |
| HBO Max / Prime Video | 4 | Browser extension userscript | (manual → `generic-csv`) | (future) |
| Hulu/Disney+/Peacock/Paramount+ | 4 | GDPR-only one-shots | (manual → `generic-csv`) | (future) |
| **Pocket** | — | **Dead. Redirect to Readwise Reader/Instapaper/Raindrop.io migration.** | n/a | (mention in setup) |

---

## 2. Periodic-update architecture

### 2.1. Watermark layer

`state.watermarks` (already on the dataclass, currently unused) is a `dict[str, str]` keyed by importer name. The value's semantics depend on the importer's natural cursor:

| Importer category | Watermark format | Example |
|---|---|---|
| API poll (lastfm, trakt, future spotify-api) | ISO 8601 of latest item's `start_time` | `"2026-05-17T08:42:00+00:00"` |
| Snapshot (apple-podcasts, netflix-csv, spotify-extended-zip) | `{path, mtime, sha16}` JSON blob | `'{"path": "/Users/.../MTLibrary.sqlite", "mtime": "...", "sha": "abcdef0123456789"}'` |
| One-shot (apple-takeout, GDPR exports) | not applicable; ignored | `null` |

A small module — `fulcra_media/watermarks.py` — provides `get(state, importer) -> str | None`, `set(state, importer, value) -> None`, and `parse_snapshot(blob) -> dict`.

For API-poll importers, the **fetch routine** accepts an optional `since: datetime | None` parameter; the **CLI** loads the watermark, passes it as `since`, and after a successful import writes the new high-water mark (max `start_time` of newly-posted items).

### 2.2. Agent-friendly outputs

Every `fulcra-media import <importer>` command gains a `--json` flag. When set, the command prints exactly one line of JSON to stdout (and nothing else to stdout — human-readable progress messages go to stderr), with this envelope:

```json
{
  "importer": "lastfm",
  "service": "lastfm",
  "ok": true,
  "total": 248,
  "skipped_existing": 0,
  "posted": 248,
  "verified": 248,
  "since_watermark": "2026-05-16T08:00:00+00:00",
  "new_watermark": "2026-05-17T08:42:00+00:00",
  "errors": []
}
```

On failure, `ok: false`, `errors` is a list of `{"stage": "fetch|normalize|post|verify", "message": "..."}`. Non-zero exit code.

Click's `--json` flag is a "global" pattern — applied to each `import_*` command via a shared `@common_options` decorator that lives in `fulcra_media/cli_common.py`.

### 2.3. Check-only mode

A `--check-only` flag does the fetch + normalize + dedup-against-existing steps, but stops before posting. Output (JSON or human) reports:

```json
{
  "importer": "lastfm",
  "would_post": 248,
  "since_watermark": "2026-05-16T08:00:00+00:00",
  "candidate_watermark": "2026-05-17T08:42:00+00:00",
  "no_new_data": false
}
```

This lets an agent cheaply detect "is there anything to import?" without paying the ingest cost.

### 2.4. Periodic invocation pattern (for agents)

Cookbook example for an openclaw-style agent:

```python
# Daily for each service the user has set up:
result = run(["fulcra-media", "import", "lastfm", "--json"], capture_output=True)
data = json.loads(result.stdout)
if not data["ok"]:
    notify(f"Last.fm import failed: {data['errors']}")
elif data["posted"] == 0:
    log(f"Last.fm: no new plays since {data['since_watermark']}")
else:
    log(f"Last.fm: +{data['posted']} new plays (now caught up to {data['new_watermark']})")
```

The `--check-only` variant is suitable for higher-frequency polling (e.g. every 5 minutes) without ingest churn:

```python
check = json.loads(run(["fulcra-media", "import", "lastfm", "--check-only", "--json"]).stdout)
if check["would_post"] > 0:
    run(["fulcra-media", "import", "lastfm", "--json"])
```

---

## 3. Last.fm importer

### 3.1. Auth model

Last.fm offers two auth flavors:
- **API key only** — read-only access to *public* scrobbles. Sufficient for any user who hasn't set their account to private. No OAuth dance; user just provides their username.
- **Full OAuth** — read private scrobbles, write loved/unloved tracks. Requires session keys + signatures.

**Decision:** Default to API key + username (public-scrobble path). It's by far the most common case and avoids OAuth entirely. Surface OAuth as a future enhancement if a user with a private profile asks for it.

Credentials file: `~/.config/fulcra-media/lastfm.json`

```json
{
  "username": "<user>",
  "api_key": "<key>"
}
```

The wizard walks the user through https://www.last.fm/api/account/create to mint a free API key. We do NOT ship a default key (rate-limit isolation per user).

### 3.2. Module layout

`fulcra_media/importers/lastfm.py`:

```python
def load_creds() -> dict
def fetch_recent_tracks(creds: dict, *, since: datetime | None = None, until: datetime | None = None, limit: int = 200, max_pages: int | None = None) -> Iterator[dict]
def normalize_track(track: dict) -> NormalizedEvent | None  # returns None for nowplaying items
def normalize_history(items: Iterable[dict]) -> Iterator[NormalizedEvent]
```

### 3.3. API mechanics

- **Base URL:** `https://ws.audioscrobbler.com/2.0/`
- **Endpoint params:** `method=user.getRecentTracks&user={username}&api_key={key}&format=json&limit={limit}&page={page}` with optional `from={unix}` and `to={unix}` for incremental.
- **Pagination:** `limit` up to 200, walk `page` 1..N. Response includes `@attr.totalPages`.
- **Rate limit:** ~5 req/sec per IP; we add 250ms sleep between pages.
- **Nowplaying items:** lack a `date` field; sit at the top of the response. The normalizer must skip them so we don't emit a half-event we'll never close.
- **Historical timestamps:** scrobbles older than ~2 weeks are stable; within the last 30 days they can be reordered. The watermark policy must use a *small backwards window* (e.g. fetch from `watermark - 1 hour`) to catch late reorderings, and rely on source-id dedup to avoid double-posting.

### 3.4. NormalizedEvent shape

```python
NormalizedEvent(
    importer="lastfm",
    service="lastfm",
    category="listened",
    note=f"{artist} – {track}",
    title=track,
    start_time=<from track["date"]["uts"]>,
    end_time=<start + 1s sentinel>,
    deterministic_id=f"com.fulcra.media.lastfm.v1.<sha256(artist|track|uts)[:16]>",
    timestamp_confidence="high",
    external_ids={
        "artist": artist,
        "track": track,
        "album": album or None,
        "url": track_url,
        "mbid": mbid or None,
        "content_fingerprint": content_fingerprint("music", artist=artist, track=track),
    },
)
```

Notes:
- No duration data → 1-second sentinel (same as Spotify IFTTT, same as Netflix slim).
- `timestamp_confidence="high"` — Last.fm scrobbles are real timestamps from the playback client.
- The MusicBrainz ID (`mbid`) when present is a fantastic cross-source dedup key. Surface it in external_ids so a future twin-dedup pass can use it in addition to content_fingerprint.

### 3.5. Watermark policy for Last.fm

- On first run: watermark is null → fetch all available scrobbles (paginated). For accounts with 100k+ scrobbles this is slow; offer `--max-pages N` to cap.
- On subsequent runs: load watermark, set `from = watermark - 1 hour` (catches late reorderings), fetch only that window.
- After successful post: set watermark = max(item.start_time) of all posted items.

---

## 4. Decision-tree wizard for service selection

The current `fulcra-media wizard <service>` subcommands assume the user already knows which service they want. A new top-level `fulcra-media setup` command walks the user through *picking* services first:

```
$ fulcra-media setup
What media services do you want to track?
  [ ] Music (Spotify, Apple Music, Last.fm, ...)
  [ ] TV / Movies (Netflix, Hulu, Disney+, Trakt, ...)
  [ ] Podcasts (Apple Podcasts, Spotify, Overcast, ...)
  [ ] Books / Reading
  [ ] Self-hosted (Plex, Jellyfin)

Selected: Music, TV / Movies

→ For Music, here are your options ranked by ease/quality:
  1. Last.fm scrobbling (recommended for ongoing — works with Spotify, Apple Music, Tidal, ...)
  2. Spotify Extended GDPR (one-shot, full history)
  3. Spotify → IFTTT → Google Drive (legacy, if you set it up)
  4. Generic CSV (you bring the data)

  Which would you like to set up? [1]
  → Walks the user through `fulcra-media wizard lastfm`

→ For TV / Movies:
  1. Trakt (recommended — covers Netflix, Hulu, Disney+, Apple TV via the Trakt apps)
  2. Netflix takeout (one-shot, full history)
  3. Apple Privacy export (Apple TV history, one-shot)
  ...
```

The decision-tree pulls from the service landscape table; it's data-driven rather than a series of hand-written prompts. New services entered into the table show up in `setup` automatically.

This is the user-facing payoff of the research: a coherent recommendation engine instead of "go read the README to figure out which wizard to run."

---

## 5. Implementation steps

### Task 1: Watermark helper module

**Files:**
- Create: `fulcra_media/watermarks.py`
- Test: `tests/test_watermarks.py`

- [ ] **Step 1: Write failing tests**

```python
from fulcra_media.state import State
from fulcra_media import watermarks

def test_get_returns_none_when_unset():
    s = State()
    assert watermarks.get(s, "lastfm") is None

def test_set_and_get_roundtrip():
    s = State()
    watermarks.set_iso(s, "lastfm", datetime(2026, 5, 17, 8, 42, tzinfo=timezone.utc))
    assert watermarks.get_iso(s, "lastfm") == datetime(2026, 5, 17, 8, 42, tzinfo=timezone.utc)

def test_get_iso_invalid_returns_none():
    s = State(watermarks={"x": "not a date"})
    assert watermarks.get_iso(s, "x") is None
```

- [ ] **Step 2: Run, expect ImportError**

`pytest tests/test_watermarks.py -v` → fails with `ModuleNotFoundError: fulcra_media.watermarks`.

- [ ] **Step 3: Implement**

```python
# fulcra_media/watermarks.py
"""Per-importer watermark management on top of state.watermarks."""
from __future__ import annotations
from datetime import datetime
from .state import State

def get(state: State, importer: str) -> str | None:
    return state.watermarks.get(importer)

def set_(state: State, importer: str, value: str) -> None:
    state.watermarks[importer] = value

def get_iso(state: State, importer: str) -> datetime | None:
    raw = state.watermarks.get(importer)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None

def set_iso(state: State, importer: str, value: datetime) -> None:
    state.watermarks[importer] = value.isoformat()
```

- [ ] **Step 4: Run tests** → 3 pass.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/watermarks.py tests/test_watermarks.py
git commit -m "feat: watermark helper for incremental imports"
```

### Task 2: --json and --check-only common decorator

**Files:**
- Create: `fulcra_media/cli_common.py`
- Test: `tests/test_cli_common.py`

- [ ] **Step 1: Failing test**

```python
import click
from click.testing import CliRunner
from fulcra_media.cli_common import emit_result

def test_emit_result_json_mode(capsys):
    emit_result(
        {"importer": "x", "ok": True, "posted": 3, "skipped_existing": 0},
        json_mode=True,
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"importer": "x", "ok": True, "posted": 3, "skipped_existing": 0}
    assert captured.err == ""

def test_emit_result_human_mode(capsys):
    emit_result({"importer": "x", "posted": 3}, json_mode=False)
    out = capsys.readouterr().out
    assert "posted=3" in out
```

- [ ] **Step 2: Run, fail.**

- [ ] **Step 3: Implement**

```python
# fulcra_media/cli_common.py
"""Shared CLI plumbing for agent-friendly output."""
import json
import sys
import click

def emit_result(result: dict, *, json_mode: bool) -> None:
    if json_mode:
        click.echo(json.dumps(result), nl=True)
    else:
        click.echo(
            " ".join(f"{k}={v}" for k, v in result.items() if k not in ("errors",)),
        )
```

- [ ] **Step 4: Run tests** → pass.

- [ ] **Step 5: Commit**

### Task 3: Last.fm fetch + normalize functions (no CLI yet)

**Files:**
- Create: `fulcra_media/importers/lastfm.py`
- Test: `tests/test_lastfm_importer.py`
- Test fixture: `tests/fixtures/lastfm_recent_tracks.json` (real-shape sample, 5 tracks + 1 nowplaying)

- [ ] **Step 1: Build the fixture**

```bash
# Save a real getRecentTracks response (anonymized): one nowplaying at top,
# four regular scrobbles, two with album.mbid, one with no album.
# Hand-edit from the Last.fm API docs sample.
```

- [ ] **Step 2: Write failing tests**

```python
def test_normalize_track_skips_nowplaying():
    track = {"name": "x", "artist": {"#text": "y"}, "@attr": {"nowplaying": "true"}}
    assert normalize_track(track) is None

def test_normalize_track_builds_normalizedevent():
    # ... real shape from fixture ...
    ev = normalize_track(track)
    assert ev.importer == "lastfm"
    assert ev.note == "Steely Dan – Reelin' In The Years"
    assert ev.timestamp_confidence == "high"
    assert ev.external_ids["content_fingerprint"] == "music:steely-dan:reelin-in-the-years"
    assert ev.external_ids["mbid"] == "abc123..."  # when present

def test_normalize_history_filters_nowplaying_and_iterates():
    with open(FIXTURE) as f:
        items = json.load(f)["recenttracks"]["track"]
    events = list(normalize_history(items))
    assert len(events) == 4  # 5 input minus 1 nowplaying

def test_fetch_recent_tracks_paginates(httpx_mock):
    # Mock 2 pages; verify both fetched, totalPages respected.
    ...

def test_fetch_recent_tracks_passes_from_timestamp(httpx_mock):
    # Verify `from=<unix>` shows up in the request params when since is set.
    ...
```

- [ ] **Step 3: Implement** per the design in §3.

- [ ] **Step 4: Run tests** → all pass.

- [ ] **Step 5: Commit**

### Task 4: Last.fm CLI subcommand + wizard

**Files:**
- Modify: `fulcra_media/cli.py:1` — add `import_lastfm` command
- Create: `fulcra_media/wizards/lastfm.py`
- Test: `tests/test_lastfm_cli.py`

- [ ] **Step 1: Wizard text + click command**

```python
# fulcra_media/wizards/lastfm.py
LASTFM_STEPS = """\
Last.fm setup

  Last.fm is the canonical 'recently played' aggregator. If you've ever
  enabled Last.fm scrobbling in Spotify/Apple Music/etc., your full play
  history is there waiting.

  1. Visit https://www.last.fm/api/account/create
  2. Fill in: name='fulcra-media-helpers', description='personal media import'
  3. Save the API key it shows you (32 hex chars).
  4. Save credentials:
       mkdir -p ~/.config/fulcra-media
       cat > ~/.config/fulcra-media/lastfm.json <<EOF
       {"username": "<your-lastfm-username>", "api_key": "<the-key>"}
       EOF
       chmod 600 ~/.config/fulcra-media/lastfm.json
  5. Run:
       fulcra-media import lastfm

  Caveats:
  - Reads PUBLIC scrobbles only. If your Last.fm profile is private, set
    'Allow others to see what music I'm listening to' under your privacy
    settings, or supply OAuth creds (future enhancement).
  - First run pulls full history; can be tens of thousands of pages. Use
    --max-pages N to cap, or --since YYYY-MM-DD to start at a specific date.
"""
```

- [ ] **Step 2: CLI subcommand**

```python
@import_group.command("lastfm")
@click.option("--since", default=None, help="ISO 8601 datetime; overrides watermark")
@click.option("--max-pages", default=None, type=int, help="Cap pagination")
@click.option("--check-only", is_flag=True, help="Don't post; just count new items")
@click.option("--json", "json_mode", is_flag=True, help="Machine-readable output")
def import_lastfm(since, max_pages, check_only, json_mode):
    ...
```

- [ ] **Step 3: Tests** — use `pytest-mock` to mock `httpx.get` so tests are hermetic. Cover: cold-start (no watermark), incremental (watermark present), --check-only doesn't POST, --json output shape, --since overrides watermark.

- [ ] **Step 4: Commit**

### Task 5: Wire --json and --check-only into existing importers (lastfm pattern as template)

For each of: `import_netflix`, `import_trakt`, `import_apple_podcasts`, `import_apple_podcasts_timemachine`, `import_spotify_extended`, `import_apple_takeout`, `import_spotify_ifttt`, `import_generic_csv`:

- [ ] Add `--json` and `--check-only` flags.
- [ ] Wrap the result printing through `cli_common.emit_result`.
- [ ] For API-poll importers (trakt currently has no watermark — add one), thread the watermark through.

One commit per importer to keep the diff readable.

### Task 6: Snapshot watermark for Apple Podcasts

Apple Podcasts is the only snapshot importer that runs periodically (the others are GDPR-style one-shots). Add `--check-only` semantics for it: hash the live `MTLibrary.sqlite` and compare to the stored snapshot watermark.

- [ ] Watermark shape: `{"sha256": "<hex of canonical DB content>"}`. Stored as JSON-encoded string in `state.watermarks["apple-podcasts"]`.
- [ ] If the hash matches, `--check-only` returns `would_post: 0`.
- [ ] Otherwise, run the snapshot, count new events (vs. existing source IDs in the time window), report.

### Task 7: Top-level `setup` decision tree

**Files:**
- Create: `fulcra_media/setup_wizard.py`
- Modify: `fulcra_media/cli.py` — add `setup` subcommand

- [ ] **Step 1: Data-driven service catalog**

```python
# fulcra_media/service_catalog.py
SERVICES = [
    {"key": "lastfm", "category": "music", "pathway": "api",
     "rank": 1, "wizard": "lastfm", "import_cmd": "lastfm"},
    {"key": "spotify-extended", "category": "music", "pathway": "gdpr",
     "rank": 2, "wizard": "spotify", "import_cmd": "spotify-extended"},
    {"key": "trakt", "category": "tv", "pathway": "api", "rank": 1, ...},
    ...
]
```

- [ ] **Step 2: Interactive picker**

Click multi-select prompt for categories → ranked list per category → drop into the appropriate wizard. Falls through to `fulcra-media import <cmd>` at the end.

- [ ] **Step 3: Tests** — use `click.testing.CliRunner` with `input="...\n"` to simulate user input.

### Task 8: Documentation

- [ ] Update top-level `README.md` with the agent-friendly invocation cookbook from §2.4.
- [ ] Add a new section "For agentic workers" explaining the JSON envelope.
- [ ] Update the service landscape doc with the populated table once research returns.

---

## 6. Open questions

| Question | Default decision | Re-open if |
|---|---|---|
| Ship a default Last.fm API key? | No — user creates their own | Most users find the API-key step a friction point |
| OAuth for private Last.fm profiles? | Defer | A user with a private profile asks |
| Watermark backward window for late-reordering catches | 1 hour | Last.fm support data suggests another value |
| `--check-only` rate-limit guard | Caller responsibility | We see users polling at <1 min cadence |
| Snapshot watermark for Netflix/Spotify-Extended (one-shot zips)? | Skip — these are not periodic | A user asks for re-detection |
| Cross-batch twin dedup via local cache | Out of scope (separate plan) | The cache plan ships |

---

## 7. Self-review check

After research returns and §1 is populated:
- [ ] Every service in §1 has a corresponding entry in §7 Task 7 catalog (or is explicitly marked "no viable path, falls through to generic-csv").
- [ ] Every `--json` envelope field is documented.
- [ ] The watermark policy for each importer category is unambiguous.
- [ ] The setup wizard's decision tree is deterministic given the catalog.

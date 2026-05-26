# Plan: Onboarding clarifications + How-do-I-get reference (2026-05-26)

User-driven follow-ups from the QA pass. Three tasks, ordered by isolation: smallest mechanical change first, deep research draft last.

## Context for every subagent

You are working in `/Users/Scanning/Developer/fulcra-tools`. The repo is a uv workspace; tests live under each package's `tests/`. Per the user's standing rule, **do not commit** at the end of your task — leave the change unstaged-or-staged for the user to review.

Tests are run with `uv run --frozen pytest tests/` from inside the relevant package directory (NOT from the repo root — that breaks because pytest-mock isn't workspace-installed). Expected pre-existing errors: `fixture 'mocker' not found` in trakt/strava/attention-cli tests; these are NOT regressions, they're env-only.

The daemon is currently running on port 9292. Don't restart it unless you need to test your change live. If you do, `pkill -9 -f 'fulcra-collect daemon' && (uv run fulcra-collect daemon > /tmp/d.log 2>&1 &) && sleep 5` works (SO_REUSEPORT was added today so restart-immediate works).

For docs/copy work, write in the same voice as the existing plugin descriptions in `packages/media-helpers/fulcra_media/collect_plugins.py` and the wizard step body_md fields — direct, second-person, lowercase imperatives, terse. No emoji, no marketing voice.

## Task 1 — Apple Podcasts on-device wizard: FDA is optional, app doesn't need to be open

**File**: [packages/media-helpers/fulcra_media/collect_plugins.py](packages/media-helpers/fulcra_media/collect_plugins.py) — locate the `APPLE_PODCASTS_PLUGIN = Plugin(...)` constructor and its `setup_steps` (search for the plugin id `apple-podcasts`).

**Current copy** asserts Full Disk Access is required and implies Apple Podcasts.app must be open. Both wrong per the prior session's pending task #74 in [SESSION_HANDOFF.md](SESSION_HANDOFF.md) and verified again today: I ingested 752 episodes from `~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite` without granting FDA explicitly to the daemon (the terminal had it, but the read worked) and the user has Podcasts.app running, not opened-specifically-for-this.

**Acceptance**:
1. Plugin `description` updated: drop the "Requires Full Disk Access" framing. Replace with something like "Reads the local Apple Podcasts SQLite database directly. Works as long as the daemon can read `~/Library/Group Containers/…/MTLibrary.sqlite` — FDA helps but isn't strictly required if the daemon already has read access via another mechanism."
2. The `permission_request` setup step (FDA grant) is changed from `required=True` to optional / softer wording: "Grant Full Disk Access (recommended)" — body_md explains it's a fallback path when sandboxing blocks the direct read; if the test verifies the read succeeds without it, Next stays unblocked.
3. Add a note in the intro step's body_md: "Apple Podcasts.app doesn't need to be open while we run, but iCloud sync stays fresh only while the system can run the Podcasts extension in the background — so completely quitting the app for days may leave the DB stale."
4. **No code-side rejection if FDA is missing** — the existing `_run_apple_podcasts` raises a clean error if the DB read fails. Wizard copy should not lie about it.
5. **Tests**: any plugin-contract assertions on `required_permissions` need updating to match the new tuple/list. Run `uv run --frozen pytest packages/media-helpers/tests/test_collect_plugins.py -k apple_podcasts -q` from inside `packages/media-helpers/`. All Apple-Podcasts tests must pass.

**Out of scope**: don't rebuild the permission_request step's UX itself; don't touch the on-device plugin's importer logic; don't touch the Time Machine recovery variant.

## Task 2 — Day One live_app wizard: clarify "app stays open" expectation

**File**: same `collect_plugins.py` would normally hold this, but Day One's plugin lives in [packages/dayone/fulcra_dayone/collect_plugin.py](packages/dayone/fulcra_dayone/collect_plugin.py) — locate `Plugin(id="dayone", ...)` and its `setup_steps`.

**Research finding (provided)**: Day One.app is currently running on this Mac and the DB at `~/Library/Group Containers/5U8NS4GX82.dayoneapp2/Data/Documents/DayOne.sqlite` is present + readable. The plugin reads the SQLite directly so the app technically doesn't need to be foregrounded at run time. **But**: the SQLite is only kept current by Day One.app receiving Day One Sync push events. If the app is completely quit for an extended period, new journal entries from other devices won't be locally readable until the app comes back up.

**Acceptance**:
1. The Day One plugin's `description` adds a sentence: "Day One.app doesn't need to be in the foreground, but should be allowed to run in the background — sync from other devices only lands locally while the app can receive push events."
2. The wizard's mode-picker step (`local_db` setting, see [collect_plugin.py:131](packages/dayone/fulcra_dayone/collect_plugin.py:131) — already labeled with friendly enum labels per the earlier fix) gets a body_md addition under the `intro` step body_md: same "stays open in the background" note.
3. The `export_file` mode is unaffected by this — its description should remain as-is (the user uploads a one-shot JSON export, the app's state is irrelevant).
4. **Tests**: `uv run --frozen pytest packages/dayone/tests/ -q` from inside `packages/dayone/`. All must pass.

**Out of scope**: don't change the underlying live_app importer; don't add explicit "is the app running" probes (out of scope for this session — that's its own task if we want a permission_check-style verifier).

## Task 3 — "How do I get my data from <source>?" reference doc

**File**: create `docs/how-do-i-get-my-data.md` (new file).

**Goal**: A single markdown page enumerating every data source fulcra-collect currently supports and the pathways into Fulcra we know about, organized so a non-dev user can find the one that matches their setup. The user's example shape: "Apple Podcasts → Live Sync → Fulcra Collect Podcasts Plugin running on a mac; Historical → Time Machine recovery via fulcra-collect's apple-podcasts-timemachine plugin." Multiple pathways per source are normal.

**Method (deep research, draft only)**:
1. **Discover every supported source** by reading the plugin registry: walk `packages/*/`*`/collect_plugin.py` and `packages/media-helpers/fulcra_media/collect_plugins.py` for `Plugin(id=..., name=..., description=...)` constructors. Each one's `description` already encodes some of the pathway info — use it as a starting point.
2. **For each source, enumerate pathways** including (where applicable):
   - **Live / continuous**: extension, webhook receiver, on-device DB read.
   - **Scheduled poll**: API-with-credentials (Last.fm, Deezer, Trakt), RSS-based polling (Goodreads, Letterboxd, Generic RSS).
   - **One-time historical import**: takeout/export upload (Spotify Extended, YouTube, Netflix, Apple TV, Day One export), Time Machine recovery (Apple Podcasts).
   - **Third-party scrobblers / aggregators**: many music services flow through Last.fm; many video services flow through Trakt — note these in the relevant source's section even when fulcra-collect doesn't have a direct plugin.
3. **Per-source schema**: Source name (H2). Then a bulleted list, each bullet a pathway with:
   - Pathway name (bold).
   - Plugin id in the daemon if any (e.g. `apple-podcasts`).
   - What the user needs (FDA? account? API key? exported file?).
   - Caveats (e.g. "requires the source app running in the background", "Spotify takes ~30 days to fulfil GDPR export request").
   - Whether it's "live" / "scheduled" / "one-shot historical".
4. **For sources where we know of a path that ISN'T currently implemented as a plugin**, add it with a "Not yet supported — open issue" marker so the doc doubles as a roadmap reference.
5. **At the top**, write a short intro (3–5 sentences) explaining the doc's purpose and the basic taxonomy (live / scheduled / historical).
6. **At the bottom**, add a "Last verified" footer with today's date so future drift is visible.

**Sources to cover** (non-exhaustive seed list — search the codebase for more):
- Apple Podcasts (on-device live, Time Machine recovery)
- Spotify (GDPR extended history, possibly IFTTT/Pipedream scrobbling, possibly Last.fm)
- Apple Music (Last.fm scrobbling — no direct plugin AFAIK)
- Deezer (OAuth API poll)
- Last.fm (the universal music sidecar — both a destination AND a source for fulcra-collect)
- Trakt (OAuth + universal video sidecar)
- Netflix (CSV download from netflix.com/Activity → upload)
- YouTube (Google Takeout → upload)
- Apple TV (Apple Data & Privacy takeout → upload)
- Letterboxd (RSS poll, no API key)
- Goodreads (RSS poll, no API key)
- Plex / Jellyfin (webhook receiver — server-side)
- Day One (live_app local DB, export_file ZIP upload)
- Generic RSS / Atom (anything with a feed URL)
- Generic media CSV (anything you can shape into a CSV)
- Browser activity (Fulcra Attention extension)

**Acceptance**:
1. `docs/how-do-i-get-my-data.md` exists.
2. Every plugin currently in the daemon's registry appears under at least one source. Walk the source files to enumerate; don't hallucinate plugins.
3. Pathways are grouped by source, not by pathway-type — a user looking for "where can my Spotify data come from" finds everything in one place.
4. Where you make a claim about a third-party service (e.g. "Spotify takes 30 days for GDPR export"), it's traceable to the plugin description that says so, OR you mark it explicitly as a best-guess.
5. The doc is linked from `SESSION_HANDOFF.md`'s "Key files" section.

**Out of scope**: do not wire this up as an in-app route or HTML page. Doc only. Implementation as a UI is its own future task.

**Caveat for the implementer**: this is the largest of the three tasks. Read every relevant source file. Don't fabricate pathways. If you're not sure whether something works, mark it as "needs verification" rather than asserting.

## Coordination notes

- Tasks 1, 2, 3 are independent enough to dispatch sequentially without conflict. Task 3 touches a new file in `docs/`; tasks 1+2 each touch one plugin definition file in different packages.
- After each task, dispatch the spec-compliance reviewer and then the code-quality reviewer per the skill's normal flow.
- **No commits**. Working tree stays dirty for the user to review.

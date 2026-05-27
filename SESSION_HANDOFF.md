# Fulcra Collect — Session Handoff (2026-05-27, end of session before user reboot)

Pick up where we left off. This file is the briefing for the next Claude session.

## Folder

```
/Users/Scanning/Developer/fulcra-tools
```

## State at handoff

- **Branch:** `session/2026-05-26-account-switch-fixes-and-qa`, working tree CLEAN.
- **All work PUSHED to origin.** HEAD: `f400cc8`. 10 commits ahead of `b522b23` (last prior-session commit). See `git log b522b23..HEAD` for the full set.
- **Daemon:** stopped before reboot. Start with `uv run --directory packages/collect fulcra-collect daemon` from the repo root. Bound to `127.0.0.1:9292`. SO_REUSEPORT means restart-immediate works.
- **Tests:** **1432 pass** across the workspace (collect 359, common 67, media-helpers 653, attention 131, dayone 43, menubar 106, csv-importer 73) + 1 expected skip (real Netflix takeout absent).
- **Extension:** built at `packages/attention/chrome/dist/`, paired in the user's Arc browser. Pairing state persists across daemon restarts via `chrome.storage.local`.
- **SQLite state migration complete.** `~/.config/fulcra-collect/state.db` (16K, schema_version 1+2). 7 legacy per-plugin JSON files renamed to `*.json.migrated`. Delete after a soak period.
- **User has pinned favorite:** the Attention def `b331bb73-aff3-41b7-b8c6-50a70126c3a7` in `~/.config/fulcra-collect/quick_record_favorites.json`.
- **User's Apple Takeout** at `/Users/Scanning/Desktop/Apple Takeout.zip` (184 MB) — importers verified against it but actual ingestion deferred to the cleanup-and-retry-onboarding flow.

## What landed in this batch of work (since the morning handoff)

Six numbered task batches, the last three driven by subagents:

### Earlier today (covered in the prior handoff text, retained here for cross-ref)

- **Account-switch hazard generalisation** (F1–F5) + **Phase-2 re-QA** (Q1–Q6) → see git log + the prior handoff `git show` for detail.

### This evening's batches

- **Batch 1 (#31, #46):** Generic-CSV `read` enum + Trakt wizard copy fixes.
- **Batch 2 (#43, #44, #45):** Skip-plugin scope fix (was already in `b522b23`), dashboard Disable button, recent-definition AttributeError fix.
- **Batch 3 (#26, #27):** Markdown fenced-code rendering via `marked@15.0.12` from cdn.jsdelivr (SRI hash now pinned alongside the script tag), Attention install copy rewritten for end-users.
- **Batch 4 (#48):** Configure wizard pre-fills existing settings + credentials. New `_loadExisting()` + `_credPresent` sentinel; empty credential inputs no longer wipe the keychain when the user is just changing other fields.
- **Batch 5 (#47):** Editable annotation name on the "Create new" path. New `PluginState.override_definition_name` (one-shot), `bind_definition` accepts `new_name`, wizard input pre-fills `canonical_definition_name`. Path A (state-carries-name) chosen for smaller diff; trade-off documented in `bind_definition` docstring.
- **Batch 6 (#42):** Settings page for soft-deleting annotation tracks. New `DELETE /api/definitions/{def_id}` route (clears any plugin state bound to the deleted def + reports cleared plugins back in the response), new `settings.js` Alpine component, new `route === 'settings'` template, dashboard header link.

### Maintenance

- **pytest-mock dep added** to `packages/collect/pyproject.toml`. `uv sync --all-packages --all-extras` brings everything in line. Re-run any test suite cleanly after this.
- **Full test sweep:** 1179 passing, 1 skipped (Netflix takeout absent on this machine).
- **Ruff:** clean across all touched Python files.
- **JS syntax-check:** clean across all `packages/web-ui/dist/static/*.js`.
- **SRI hashes** pinned alongside the marked@15.0.12 and alpinejs@3.14.1 CDN script tags in `index.html` — closes the two pre-production TODOs that were in the head section.
- **definition_picker render-site sync:** the dashboard-Configure flow now has full feature parity with the onboarding flow (per-def selection checkmark, recent-entries preview on selection, identical prominent "Create new" button, editable name input). A code comment at the second site flags the verbatim-duplication so future edits stay in sync.

### Apple Podcasts (#49)

- New module `apple_podcasts_health.py` with `apple_podcasts_health_check(ctx)` that opens the Podcasts SQLite DB read-only, counts played episodes (predicate matches `parse_db`'s WHERE so the count == what Run will import), and returns 3 most-recent episode titles as the preview.
- Wired into `APPLE_PODCASTS_PLUGIN` as `health_check` + a new `test_connection` setup step between `permission_request` and `definition_picker`.
- 3 new tests in `test_apple_podcasts_health.py` cover the played-episodes-present, zero-played, and DB-missing paths.

### In-app docs viewer (#51)

- Daemon: new `GET /api/docs/{name}` route returns raw markdown from `docs/<name>.md`. Path-validated (regex + resolve-relative-to defence-in-depth) and auth-gated. 4 new tests in `test_web.py`.
- Frontend: new `docs` route in app.js. `goToDocs(name, title)` fetches the markdown, sets `docsMarkdown` + `docsTitle`, and the `docs` route template renders via marked. Tailwind prose plugin isn't loaded; manual typography hints inline.
- Dashboard header: "Data sources" link dispatches `go-to-docs` with the canonical doc name. Previously linked to the github blob URL — which 404s while the repo is private — replaced with this in-app pathway.
- README.md: refreshed to list all 8 current packages (was missing `collect`, `dayone`, `menubar`, `web-ui`, `fulcra-common`) plus a top-level pointer at the doc.

### Auto-run + URL parsing + Plex cross-machine (#56)

- Wizard `done` step auto-triggers Run-now for non-service plugins on entry, polls `/api/status` for up to 10s, renders ✓/⚠️/spinner/slow banners inline. User sees "First run succeeded" or the actual error before navigating to dashboard.
- Goodreads + Letterboxd settings accept either bare ID/username OR full profile URL — `_extract_goodreads_user_id` / `_extract_letterboxd_username` parse permissively. Smoke-tested against the user's actual Goodreads URL.
- Plex/Jellyfin wizard rewritten: `setup_topology` dropdown (`same` / `lan`), conditional `input` step for LAN mode collecting `host` + `bearer-token`, two conditional `external_action` steps showing the right URL. Cross-machine URL is `http://<lan-ip>:8765/webhook?token=<bearer>` — webhook_receiver already accepted `?token=` query strings, the wizard just wasn't surfacing it. Plex wizard copy also clarified that webhooks live in the **server** settings page, not the Plex account.

### Pill mapping + attention-event state refresh

- Dashboard pill: `last_outcome="error"` with `failures<3` now renders amber "Failed — run again" instead of the misleading "Not run yet".
- Extension events: `/api/extension/attention` POSTs now refresh per-plugin state's `last_outcome="done"` / `consecutive_failures=0` so the pill reflects "events are flowing", not the diagnostic run from before the user paired.

### #30 Attention timeline 0h 0m

- Three sites where Duration annotations are built (`fulcra_attention/ingest.py`, `fulcra_media/fulcra.py`, `fulcra_csv/fulcra.py`) now include `duration_seconds` on the inner data payload. The actual timeline-renderer fix lives in `context.fulcradynamics.com` (different repo) but providing the field defensively is the most likely vector — comment at each site cross-references the task.

### #55 Cross-source content fingerprint

- New `packages/fulcra-common/fulcra_common/cross_source_fingerprint.py` with `listened_fingerprint` / `watched_tv_fingerprint` / `watched_movie_fingerprint` / `podcast_fingerprint` + `normalize_title` (strips parens, brackets, feat-suffixes, remaster-tags) + `bucket_5min` helper.
- `wire.build_record` extended with `extra_source_ids` param — flattened into `metadata.source` array between per-source `source_id` and the definition-source entry; dedupes empties.
- `NormalizedEvent.extra_source_ids` field on `importers/base.py`; every relevant importer now emits a cross-source fingerprint alongside its per-source `deterministic_id`.
- Importers wired: `lastfm`, `apple_music_takeout`, `spotify` (extended + ifttt), `apple_takeout` (3 paths: TV/movie/playback/legacy), `trakt`, `netflix` (rich path), `apple_podcasts`, `letterboxd` via new `generic_rss.extract_extra_source_ids` callback.
- Punted: `youtube` (no clean episode/movie taxonomy in Takeout — would collide on common video titles); `netflix` slim path (date-only data; never aligns with other sources).
- 30 unit tests + 14 importer-level + integration tests; positive case (Last.fm + Apple Music same listen → identical `com.fulcra.content.listened.v1.*` entry) + negative bucket-boundary case both pinned.

### Systematic QA + dead-code sweep

- All 17 plugin entry points resolve to real Plugin objects; setup_step `kind` values all in the known set; no settings/credentials key collisions.
- Every plugin's run-with-empty-config produces a friendly error (no raw stack traces in the user-facing path).
- Deleted-file references all clean; no lingering imports of removed `relay.py` / `service_manager.py`.
- 2 F841 unused-variable hits in test files fixed.
- Secret-leak scan clean: every `f"Bearer {token}"` is a legitimate Authorization header.
- E2E status reference written at `docs/E2E_STATUS.md` — per-plugin verification state.

### Open

- **#57 (deferred):** Add wizard-time `health_check` to deezer, letterboxd, goodreads, generic-rss, and the takeout-shaped plugins (netflix, spotify-extended, youtube, apple-takeout, apple-music-takeout). Apple Podcasts has one as of #49 — the pattern generalizes.

### Open tasks

- **#30** — different-repo timeline render bug (`0h 0m total`), not addressable here.
- **#49** — Apple Podcasts health_check + test_connection step (in flight via subagent at handoff time).
- **#50** — this doc refresh (in flight, the file you're reading).

All other previously-filed tasks (#14–#48 except #30) are completed.

## Resume command (paste into a fresh Claude session)

```
You are picking up a fulcra-tools session. Read /Users/Scanning/Developer/fulcra-tools/SESSION_HANDOFF.md (this file) and docs/E2E_STATUS.md (plugin verification matrix) first.

Working directory: /Users/Scanning/Developer/fulcra-tools

Branch: session/2026-05-26-account-switch-fixes-and-qa. All work is pushed to origin; working tree is clean. HEAD is f400cc8. Run `git log b522b23..HEAD` for the 10 commits added this session. 1432 tests pass across the workspace.

The daemon is NOT running — user rebooted between sessions. When you need to test, restart it yourself (per ~/.claude/CLAUDE.md "Record long-running processes I start"):

  cd /Users/Scanning/Developer/fulcra-tools/packages/collect
  uv tool install --force --editable .
  cd /Users/Scanning/Developer/fulcra-tools
  uv run --directory packages/collect fulcra-collect daemon
  # ^ via Bash run_in_background:true — capture the task ID and write to memory

Likely directions for THIS session (ASK before assuming):
1. The user's cleanup-and-retry plan: soft-delete defs from Settings → clear state → re-onboard from welcome.
2. Tackle refactor #68 (Lit setup-step components) or #69 (unified ingest pipeline) — plans in docs/plans/.
3. Walk through the Apple takeout ingestion (file at ~/Desktop/Apple Takeout.zip, importers ready).
4. Whatever new bugs emerge from the user testing the latest pushed code.

Defer #30 (timeline render bug) — different repo.
```

## Key files (post-refactor)

- **Daemon HTTP — app factory + cookie + static + serve():** `packages/collect/fulcra_collect/web.py` (only 319 lines after refactor B)
- **HTTP route modules** (one per coherent slice): `packages/collect/fulcra_collect/routes/{status,plugins,definitions,fulcra_auth,oauth,activity,docs,annotations,extension,menubar}.py` plus `_deps.py` (RouteContext + Pydantic body models)
- **Daemon core:** `packages/collect/fulcra_collect/daemon.py` (1001 lines — refactor #1 Phase 2 will trim when per-package state moves to SQLite)
- **SQLite state store:** `packages/collect/fulcra_collect/db.py` (connection lifecycle + migrations)
- **Per-plugin state shim:** `packages/collect/fulcra_collect/state.py` (thin SQLite wrapper, preserves the PluginState dataclass + load/save API)
- **Plugin contract:** `packages/collect/fulcra_collect/plugin.py`
- **Plugin definitions** (post-refactor — one file per plugin):
  - `packages/media-helpers/fulcra_media/plugins/<id>.py` for the 15 media-helpers plugins (lastfm, deezer, trakt, netflix, spotify_extended, youtube, spotify_ifttt, apple_takeout, apple_music_takeout, generic_rss, letterboxd, goodreads, apple_podcasts, apple_podcasts_timemachine, generic_csv, media_webhook)
  - `packages/media-helpers/fulcra_media/plugins/_common.py` for shared helpers (ensure_media_def, RSS pipeline, file-import dispatch)
  - `packages/media-helpers/fulcra_media/collect_plugins.py` is a 99-line back-compat shim re-exporting the *_PLUGIN constants
  - Attention: `packages/attention/fulcra_attention/collect_plugin.py`
  - Day One: `packages/dayone/fulcra_dayone/collect_plugin.py`
- **Health checks** (10 plugins now have them): `packages/media-helpers/fulcra_media/{lastfm,trakt,deezer,apple_podcasts,rss,takeout,feed_plugin}_health.py`
- **Cross-source fingerprint:** `packages/fulcra-common/fulcra_common/cross_source_fingerprint.py` (listened/watched/podcast emitters that share a fingerprint across importers so Fulcra's source_id dedup catches the same listen from multiple sources)
- **Wizard frontend:** `packages/web-ui/dist/static/{app,dashboard,onboarding,wizard,settings}.js` + `packages/web-ui/dist/index.html`
- **Browser extension:** `packages/attention/chrome/` (Vite/React; `npm run build` outputs to `dist/`)
- **Worker adapter:** `packages/collect/fulcra_collect/worker.py`
- **Shared Fulcra API client:** `packages/fulcra-common/fulcra_common/client.py` (carries `soft_delete_definition`, `definition_exists`)
- **Menubar daemon-lifecycle controls:** `packages/menubar/fulcra_menubar/daemon_lifecycle.py` + `popover/daemon_bar.py`
- **Quick-record favorites storage:** `packages/collect/fulcra_collect/quick_record_favorites.py` (local file at `~/.config/fulcra-collect/quick_record_favorites.json`)
- **Data-source pathway reference:** `docs/how-do-i-get-my-data.md` (served in-app via `/api/docs/how-do-i-get-my-data`)

## Memory entries worth knowing

- `project_fulcra_tools_dev.md` — daemon invocations, menubar icon shape, expected dev-mode warnings.
- `feedback_alpine_xdata_remount.md` — Alpine 3 gotcha (re-init by toggling through null).
- `feedback_account_switch_caches.md` — every plugin caching def_id/tag_id has the same orphan-ingest hazard; layered fixes at choke point + ensure_definition + startup pre-flight.
- `reference_fulcra_create_def_schema.md` — POST /user/v1alpha1/annotation needs `tags: []` + `description: ""` even on duration defs; adapter default-injects at worker.py:55.
- `reference_fulcra_api.md` — canonical Fulcra URLs.

## Architecture lessons (carried forward + this batch)

1. **State caches survive auth changes; APIs do not.** Multi-layer fix: per-call validation at choke point, per-package callers using `ctx.ensure_definition`, proactive daemon-startup invalidation when the bearer-token changes.
2. **Per-plugin and per-package state files must stay in sync.** Wizard's `definition_picker` writes per-plugin state; plugins like Attention read per-package state. Fixed via lazy-migration in the extension route (#29); the two-store design still deserves a refactor.
3. **Fulcra schema drift.** Defaults injected at the adapter layer. Worth a contract test against the live API so the next drift surfaces immediately (still pending).
4. **Activity feed is the best debug surface.** Failures invisible in the timeline UI are immediately visible there.
5. **Definition picker's "Create new" is now editable + one-shot.** State carries `override_definition_name`; resolver uses it verbatim (no machine-id suffix) then clears it so a future re-resolve falls back to canonical-name + suffix.
6. **Soft-delete clears bound plugin state.** `DELETE /api/definitions/{id}` walks every plugin and zeroes `state.definition_id` on matches, then reports the cleared plugin IDs in the response — so a future re-resolve doesn't keep posting events to a tombstone.

## Push state

Everything from this session IS pushed to `origin/session/2026-05-26-account-switch-fixes-and-qa` (HEAD `f400cc8`). The branch hasn't been merged into `main` yet — the user reviews before merge. When the user is ready to merge, they'll either squash-merge the 10-commit session or merge as-is; either is fine since each commit's message tells the whole story.

The pre-push orphan/obsolete review + secret-leak scan (per `~/.claude/CLAUDE.md` global rules) was done before each push this session. Both pushes were clean.

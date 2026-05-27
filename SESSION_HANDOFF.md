# Fulcra Collect — Session Handoff (2026-05-26 — late)

Pick up where we left off. This file is the briefing for the next Claude session.

## Folder

```
/Users/Scanning/Developer/fulcra-tools
```

## State at handoff

- **Branch:** `session/2026-05-26-account-switch-fixes-and-qa`, working tree dirty (12 modified files, 1 new file, no commits yet on this branch beyond the prior commits documented below).
- **Nothing committed, nothing pushed since the last review.** The user reviews before pushing.
- **Daemon:** stable port 9292. Start with `uv run fulcra-collect daemon` from the repo root. URL is mirrored to `~/.config/fulcra-collect/web-url`. SO_REUSEPORT is set so restart-immediate works — no more 60-90s TIME_WAIT pain.
- **Tests:** **1278 pass** across the workspace (collect 299, common 67, media-helpers 628, attention 131, dayone 43, menubar 37, csv-importer 73) + 1 expected skip (real Netflix takeout absent). **The previously-flaky 5 `pytest-mock` fixture errors are GONE — `pytest-mock>=3.12` is now in `packages/collect/pyproject.toml`.**
- **Extension:** built at `packages/attention/chrome/dist/`, paired in the user's testing browser. Re-paired via wizard in the prior session; pairing state persists across daemon restarts via `chrome.storage.local`.

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
You are picking up a fulcra-tools QA + dev session. Read /Users/Scanning/Developer/fulcra-tools/SESSION_HANDOFF.md (latest handoff) and CREDS_NEEDED.md (status of every plugin's verification).

Working directory: /Users/Scanning/Developer/fulcra-tools

The branch is session/2026-05-26-account-switch-fixes-and-qa. Working tree is dirty with the changes documented in the handoff. Run `git status` and `git diff --stat HEAD` to see the shape. 1176 tests pass across the workspace; pytest-mock is now in deps.

Priority order for THIS session:
1. Review the dirty working tree with the user. Decide what to commit and in what shape (suggested split: two commits — feat(picker): editable custom name on Create new + feat(settings): soft-delete annotation definitions page; everything else stays in the existing in-progress commit).
2. If user is around: ask them to spend 30s browsing in the paired Chrome browser so the chrome extension's live data path is finally verified (vs the synthetic POSTs).
3. Walk any of the live-cred plugins from CREDS_NEEDED.md if user has creds in hand (Trakt is the highest-value unverified surface).
4. Defer #30 (timeline render bug) — different repo.

Constraint: do NOT commit or push without explicit user approval. Working tree is dirty intentionally.
```

## Key files

- **Daemon HTTP:** `packages/collect/fulcra_collect/web.py` (now includes `DELETE /api/definitions/{def_id}` for soft-delete)
- **Daemon core (incl. pre-flight + activity throttle):** `packages/collect/fulcra_collect/daemon.py`
- **Plugin contract + state:** `packages/collect/fulcra_collect/plugin.py`, `packages/collect/fulcra_collect/state.py` (now carries `override_definition_name`)
- **Plugin definitions:**
  - Most plugins: `packages/media-helpers/fulcra_media/collect_plugins.py` (uses `_ensure_media_def` helper)
  - Attention: `packages/attention/fulcra_attention/collect_plugin.py`
  - Day One: `packages/dayone/fulcra_dayone/collect_plugin.py` (re-queries by name on every run)
- **Plugin health checks:** Last.fm, Trakt, Apple Podcasts (in flight via #49)
- **Wizard frontend:** `packages/web-ui/dist/static/{app,dashboard,onboarding,wizard,settings}.js` + `packages/web-ui/dist/index.html`
- **Browser extension:** `packages/attention/chrome/` (Vite/React; `npm run build` outputs to `dist/`)
- **Worker adapter:** `packages/collect/fulcra_collect/worker.py`
- **Shared Fulcra API client:** `packages/fulcra-common/fulcra_common/client.py` (carries `soft_delete_definition`, `definition_exists`)
- **Data-source pathway reference:** `docs/how-do-i-get-my-data.md`

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

## Don't push

The user reviews before pushing. Working tree is dirty intentionally.

# Fulcra Collect — Session Handoff (2026-05-27, end of second session)

Pick up where we left off. This file is the briefing for the next Claude session.

## Folder

```
/Users/Scanning/Developer/fulcra-tools
```

## State at handoff

- **Branch:** `session/2026-05-26-account-switch-fixes-and-qa`, working tree CLEAN.
- **HEAD:** `781dd4f`. **58 commits ahead of `origin/session/...`** — nothing pushed yet this session. User reviews before push.
- **54 commits** since `1a310f6` (the prior session's end). Tracked work spans 5 sub-projects: refactor #68 + refactor #69 (merged from worktrees early in the session), the new `collect_modes` onboarding screen (web UI), and four menubar sub-projects (SP1–SP4) plus follow-up commits.
- **Tests:** **1452 pass, 1 skipped** across the workspace (collect 368, common 67, media-helpers 653, attention 131, dayone 43, menubar 106, csv-importer 73, + the new ingest pipeline tests + the new contract/route tests). The previously-known SQLite-WAL-init flake (`test_concurrent_writes_from_two_threads_do_not_lose_data`) is now stable — fixed in commit `37d417e`.
- **Daemon:** running, launchd-managed at PID **34006** on `127.0.0.1:9292`. Loaded code reflects HEAD `781dd4f`.
- **Menubar:** running at PID **34021** (I started it; record at `~/.claude/projects/.../memory/project_fulcra_menubar_running.md`). Loaded code reflects HEAD `781dd4f`.
- **Browser:** Arc paired via the claude-in-chrome MCP, on the tab opened to the wizard.

## What landed this session

Ordered roughly by when they landed. Five sub-projects + a few cross-cutting follow-ups.

### Refactor #68 — Lit web components for wizard setup_step renderer

The `<template x-if="current_step.kind === '...'">` blocks duplicated across two render sites (onboarding flow + dashboard Configure flow) became `<fulcra-step-<kind>>` Lit web components, one per step kind. The dispatcher `<fulcra-step>` routes by kind. Light DOM only so Alpine's `$data` resolution still works inside the components.

Took a couple of debugging rounds — the Lit CDN bundle's bare-module-specifier issue (`@lit/reactive-element`), then a multi-layer Alpine ↔ Lit reactivity bridge: the Alpine `x-effect` evaluator only wraps top-level identifier reads in dep-tracking, not iteration-helper reads, so `Object.values($data)` didn't track. Solved by installing an `Alpine.effect` from inside the wizard's `init()` (where `this` is Alpine's reactive proxy at full strength), which dispatches a `fulcra-wizard-tick` CustomEvent that the dispatcher listens for. Plus `hasChanged: () => true` on the Lit base so prop reassignments with identity-equal refs still trigger re-renders.

Files at HEAD: 14 components in `packages/web-ui/dist/static/components/`. `index.html` shrunk from ~1965 to ~943 lines. See `docs/plans/2026-05-26-refactor-2-execution.md`.

### Refactor #69 — Unified IngestPipeline + typed IngestableEvent

Pulled all 4 `wire.build_record` callsites (media-helpers, attention, csv-importer, daemon quick-record) onto a shared `IngestPipeline` in `packages/fulcra-common/fulcra_common/ingest.py`. Byte-parity verified throughout cutover, regression test retired in Phase 3. Attention's category-variant top-level data fields stay top-level via dedicated `DurationEvent` fields (not pushed under `data.external_ids.*`).

7 commits. See `docs/plans/2026-05-26-refactor-3-execution.md` + the new `packages/fulcra-common/README.md`.

### `collect_modes` onboarding screen (web UI)

New phase between `signin` and `pick_plugins`. Title: "How to gather and update your data with Fulcra Collect." Lede explains historical-vs-live, then a 2×2 combo grid (Music: Last.fm + Apple Music takeout; TV: Trakt + Netflix takeout; Podcasts: Apple Podcasts on-device + Time Machine recovery; Movies via Apple: Trakt + Apple takeout), then an Attention callout, then encouragement to write custom plugins.

Plus a daemon-side fix: `/static/` now serves with `Cache-Control: no-cache` so frontend edits land on already-open browser tabs without hard-reload. Friction-killer that unblocked all the subsequent UI work.

5 commits. See `docs/plans/2026-05-27-onboarding-collect-modes-screen.md` (spec) + `docs/plans/2026-05-27-onboarding-collect-modes-execution.md` (plan).

### SP1 — Menubar spacing fixes

User flagged "spacing is really bad" on the menubar. A Plan agent scoped 5 user-visible surfaces, catalogued 7 drift items, and identified the worst 3 layout findings:

- L3: About tab top action row was cramming 5 visual elements into 60pt vertical. Doubled the caption→separator and separator→identity gaps to 24pt each.
- L1: Quick-record Duration row had 8pt clearance between Record and Timer (mis-click hazard). Shrunk `COMMENT_W` 140→120, gap now 24pt.
- L2: Plugins-tab description was a fixed 32pt height; long descriptions silently clipped at 2 lines. Now dynamic via `NSString.boundingRectWithSize_options_attributes_`, capped at 80pt.

Plus an orphan-sweep commit cleaning a stale ASCII y-coord diagram in `about_tab.py`. 4 commits. See `docs/plans/2026-05-27-menubar-vs-webui-drift-scope.md` §SP1 + `docs/plans/2026-05-27-sp1-menubar-spacing-execution.md`.

### SP2 — Annotation management in menubar

Brought soft-delete + bulk favorites management out of the web UI and into the menubar. 6 commits:

1. Daemon refactor: extracted `delete_definition` HTTP-route business logic into `Daemon._delete_definition(def_id) -> dict` (with `code` field — `bad_request`/`unauthorized`/`not_found`/`timeout`/`upstream_error` — so the HTTP route's status-map dispatch is stable across daemon-side error wording changes). HTTP route delegates. New UDS command `delete_definition`. 3 new tests. Shared `_in_memory_keyring` fixture lifted to `conftest.py`.
2. `DaemonClient.delete_definition` method.
3. New "Annotations" Preferences tab between Plugins and Notifications. Bulk favorites checkbox toggle + per-row Delete button with NSAlert confirmation. Caught and fixed a Critical orphan-ingest hazard during code review (transient `get_quick_record_favorites` read failure would have wiped the user's favorites list — same pattern as `feedback_account_switch_caches.md`).
4. Popover quick-record per-row "…" menu with Delete-this-track item.
5. Shared `_definition_delete.py` module — `show_delete_alert` + `_NSAlertFirstButtonReturn` constant. Replaced byte-for-byte-identical helpers in Annotations tab + popover "…" menu.

See `docs/plans/2026-05-27-sp2-annotations-management-execution.md`.

### SP3 — Historical/live framing + pill mapping unification in menubar

Surface the user-facing framing from the `collect_modes` onboarding screen across the menubar's plugin views, and unify the popover's coarse status-dot mapping with the dashboard's three-tier pill. 5 commits:

1. Plugin contract gains required `collect_mode: Literal["historical", "live_polled", "live_continuous"]` field. Validation in `__post_init__`. 3 contract tests. 18 plugin modules updated. **Attention is the only `kind="manual"` + `collect_mode="live_continuous"` case** — explicitly documented with a 7-line marker comment.
2. Daemon `_status()` propagates `collect_mode` per plugin (HTTP `/api/status` + UDS `status` both via the one shared site).
3. Menubar `PluginSnapshot.collect_mode: str` + popover plugin-status view re-grouped under `Live (continuous)` / `Live (polled)` / `Historical (one-shot)` (most-live first).
4. `_status_dot` rewritten to match `dashboard.js`'s 6-state pill (`disabled` / `failing ≥3` / `running` / `done` / `1–2 failures amber` / `not-run-yet`). Preferences → Plugins row gets a `collect_mode` chip (kind taxonomy stays — Q2 said augment, not replace).
5. Popover header subtitle drift fix (caught in SP3 final review): the header's `N scheduled · M services · X manual` summary now reads `N live · M polled · X one-shot`, matching the body's language.

See `docs/plans/2026-05-27-sp3-historical-live-framing-execution.md`.

### SP4 — In-app docs link + popover Configure/Disable

Closed the docs-link gap and the popover's "no Configure/Disable" gap. 7 commits including 4 follow-ups:

1. Web UI URL-param handler in `app.boot()`: `?route=docs[&page=NAME]` / `?route=configure&plugin=ID` / `?route=settings`. Clears params via `replaceState` to prevent reload loops. Auth-gated (unauth users see signin first; deep-link drops after auth — known v1 limitation).
2. Popover header "?" docs button between the title and status pill (gear stays at the right edge). Opens `/?route=docs` in the system browser.
3. Popover plugin row gains `[Disable] [Configure] [Run now]` action set (Configure unconditional; Disable only when enabled). Name/id column shrunk 200→108pt to fit; right-aligned status text moved to top-right.
4. **Orthogonal: SQLite WAL-init race fix.** A subagent surfaced the existing `test_concurrent_writes` flake and applied a working fix in the worktree but didn't commit. Picked it up — `db.py` `open()` wrapped in a module-level `threading.Lock()` that serialises the `PRAGMA journal_mode=WAL` switchover + the `migrate()` initial row write. Closes the only known test flake.
5. README docs in both `packages/web-ui/README.md` and `packages/menubar/README.md` describing the new deep-link contract (consumer + producer sides).
6. `_daemon_url.py` helper in the menubar that reads the daemon's well-known `~/.config/fulcra-collect/web-url` file (respects `[daemon] web_port` overrides) — replaces the initially-hardcoded `127.0.0.1:9292` URLs in the docs and Configure buttons.
7. README reconcile: the README docs commit and the helper commit landed in parallel; flipping the menubar README to describe the now-canonical helper.

See `docs/plans/2026-05-27-sp4-docs-configure-disable-execution.md`.

### Cross-cutting

- `Cache-Control: no-cache` middleware on the daemon's `/static/` mount (in the collect_modes screen sub-project but enables every subsequent frontend change).
- `_in_memory_keyring` fixture lifted from `test_web.py` to `tests/conftest.py` for cross-test-file reuse.
- WAL-init race fix in `db.py` closes the long-standing flaky test.

## Pending / not yet tested

### Manual walkthroughs the user owes

Daemon (PID 34006) + menubar (PID 34021) are both running. The user pinned the manual visual verification across these surfaces:

- **SP1 spacing fixes:** About tab top row, Quick-record Duration row, Plugins-tab description (long descriptions render in full now).
- **SP2 annotation management:** Preferences → new "Annotations" tab (favorites checkboxes + Delete + NSAlert), popover quick-record per-row "…" menu with Delete (no 56pt visual hole after delete — row reflow active).
- **SP3 framing + pill:** Popover → "View Status →" view groups by `Live (continuous)` / `Live (polled)` / `Historical (one-shot)`; status dots use the dashboard's three-tier mapping; Preferences plugin rows show `collect_mode` chip; popover header subtitle says `N live · M polled · X one-shot`.
- **SP4 docs + Configure/Disable:** Popover header "?" icon (between title and pill) opens docs in browser; each enabled plugin row shows `[Disable] [Configure] [Run now]`; each disabled row shows `[Configure]` only; deep-link URLs work end-to-end.
- **Web UI testing plan** Phases A–F (the user's original testing pass when SP1+SP2+SP3+SP4 hadn't been started yet) — including the `collect_modes` onboarding screen walkthrough and the cleanup-and-retry full onboarding (Test 4 from that plan).

### Open follow-ups — most landed at end of session

After the user asked "do all followups", four of the five reviewer-flagged minor items got addressed in three small commits at the end of the session:

- **SP3 Attention drift audit test** (commit `8e66c36`) — `test_attention_is_the_only_manual_live_continuous_plugin` walks the real production registry and asserts `attention-relay` is the sole deviation from the default `kind→collect_mode` mapping. A future plugin author copying Attention's pattern would now break this test rather than silently misroute their plugin in the popover.
- **SP4 deep-link auth-stash + popover row visual polish** (commit `bc5d893`) — three fixes bundled: (1) URL-param handler stashes the query string in `sessionStorage` when unauth so post-signin lands on the deep-link destination instead of the default route; (2) `right_text` label gets `NSLineBreakByTruncatingTail` as defensive measure against future expansion overrunning the Disable button below it; (3) `setToolTip_` on truncated plugin name + id labels so hover reveals the full string when the 108pt column truncates (mitigates the "Apple Music Takeout" visibility concern).
- **SP1 empty-description phantom-row fix** (commit `781dd4f`) — `_compute_desc_height` now returns `0.0` for empty descriptions instead of 32.0. Rows drop from `112+24*N` to `80+24*N` pt for no-description plugins; rows with descriptions are unaffected.

Still open (intentional):
- **SP1 latent humanize_caption / Run-button overlap** at the impossible-in-practice "scheduled + empty desc + zero credentials" case. Pre-existing, hidden by every real plugin having credentials.

### What's NOT in scope but worth knowing

- The user has been doing web-UI testing in parallel through much of this session but did not surface results back. Their original web-UI testing plan (Phases A–F, including the destructive cleanup-and-retry onboarding flow) was authored before the menubar work started.
- The branch hasn't been pushed. User reviews before push.

## Resume command (paste into a fresh Claude session)

```
You are picking up a fulcra-tools session. Read /Users/Scanning/Developer/fulcra-tools/SESSION_HANDOFF.md (this file) first, then docs/plans/2026-05-27-menubar-vs-webui-drift-scope.md (the source spec for SP1–SP4) and the four SP*-execution plans for full task-level detail.

Working directory: /Users/Scanning/Developer/fulcra-tools.

Branch: session/2026-05-26-account-switch-fixes-and-qa. All work is committed; nothing pushed yet. HEAD is 781dd4f. 58 commits ahead of origin. Working tree clean. Run `git log --oneline 1a310f6..HEAD` for the full set. 1452 tests pass across the workspace, 1 expected skip (Netflix takeout absent on this machine).

Daemon is launchd-managed at PID 34006 on 127.0.0.1:9292 (kickstart via `launchctl kickstart -k gui/$(id -u)/com.fulcra.collect` to pick up code changes). The menubar app is running at PID 34021 — I started it; the record at /Users/Scanning/.claude/projects/-Users-Scanning-Developer-fulcra-tools/memory/project_fulcra_menubar_running.md has the kill recipe. Both processes are on the latest code.

The user owes manual visual walkthroughs across SP1–SP4 + the web-UI testing plan + the collect_modes onboarding screen flow. AppKit layout has no autonomous test path; the user's eyes are the gate. The "Manual walkthroughs the user owes" section above lists each surface explicitly.

Most-likely next direction (ASK before assuming):
1. Push to origin so the 54 commits are backed up + the user can do the walkthrough on a confidence-boosting "I have my work safe" footing.
2. Walk through one of the SP* surfaces with the user driving the menubar / web UI.
3. Address one of the open follow-ups (Attention drift test, deep-link auth-stash, long-name truncation, etc.).
4. Tackle something the user surfaces from web-UI testing.

The branch hasn't been merged into `main`. When the user is ready to merge, they'll choose between squash-merge (one commit per sub-project) or merge-as-is (preserve the 54-commit narrative). Each commit message already tells a self-contained story, so either is reasonable.
```

## Key files (current state at HEAD `781dd4f`)

### Web UI

- `packages/web-ui/dist/index.html` (~943 lines after the Lit refactor) — root template + all 5 onboarding phases (welcome, signin, collect_modes, pick_plugins, configure, done).
- `packages/web-ui/dist/static/components/` — 14 Lit web components (the wizard's setup_step renderer): `_base.js`, `step.js` (dispatcher), plus per-kind `step-<kind>.js`.
- `packages/web-ui/dist/static/app.js` — top-level Alpine factory + URL-param handler in `boot()` (SP4 Task 1).
- `packages/web-ui/dist/static/wizard.js` — the wizard's state machine + `_installLitReactivityBridge` that drives Lit re-renders via the `fulcra-wizard-tick` CustomEvent.
- `packages/web-ui/dist/static/onboarding.js` — the 5-phase orchestrator including the new `collect_modes` phase.
- `packages/web-ui/dist/static/settings.js`, `dashboard.js` — auxiliary Alpine x-data factories.

### Collect daemon

- `packages/collect/fulcra_collect/plugin.py` — Plugin contract. New `collect_mode` field (SP3). Validation in `__post_init__`.
- `packages/collect/fulcra_collect/daemon.py` — handle_request UDS dispatch. New `delete_definition` branch (SP2). `_status()` builds the per-plugin response dict that both HTTP and UDS surfaces share — now includes `collect_mode` (SP3).
- `packages/collect/fulcra_collect/db.py` — module-level `_init_lock` serialises WAL switchover + initial migrate (WAL flake fix).
- `packages/collect/fulcra_collect/web.py` — `/static/` mount wrapped in `Cache-Control: no-cache` middleware (collect_modes sub-project).
- `packages/collect/fulcra_collect/routes/` — per-area route modules. `definitions.py` route now delegates to `daemon._delete_definition` and translates the `code` field to HTTPException status (SP2).
- `packages/collect/fulcra_collect/quick_record_favorites.py` — favorites storage at `~/.config/fulcra-collect/quick_record_favorites.json`.

### Menubar

- `packages/menubar/fulcra_menubar/_daemon_url.py` (NEW, SP4) — `daemon_url(path)` + `daemon_base_url()`. Reads `~/.config/fulcra-collect/web-url` with config fallback. Respects `[daemon] web_port` overrides.
- `packages/menubar/fulcra_menubar/_definition_delete.py` (NEW, SP2) — shared `show_delete_alert` for both Annotations Prefs tab + popover "…" menu.
- `packages/menubar/fulcra_menubar/model.py` — `PluginSnapshot.collect_mode: str` (SP3).
- `packages/menubar/fulcra_menubar/daemon_client.py` — `delete_definition` method (SP2). Plus all the existing surface (status, quick_record_list, set_quick_record_favorites, etc.).
- `packages/menubar/fulcra_menubar/popover/header.py` — title + "?" docs button + status pill + gear (SP4 Task 2).
- `packages/menubar/fulcra_menubar/popover/quick_record.py` — per-row "…" menu (SP2 Task 4).
- `packages/menubar/fulcra_menubar/popover/plugin_row.py` — `_status_dot` 6-state mapping (SP3 Task 4) + `[Disable] [Configure] [Run now]` action set (SP4 Task 3).
- `packages/menubar/fulcra_menubar/popover/root.py` — plugin-status view groups by `collect_mode` (SP3 Task 3).
- `packages/menubar/fulcra_menubar/preferences/annotations_tab.py` (NEW, SP2) — bulk favorites + soft-delete per-row.
- `packages/menubar/fulcra_menubar/preferences/plugins_tab.py` — collect_mode chip per row (SP3 Task 4) + dynamic description height (SP1 L2).
- `packages/menubar/fulcra_menubar/preferences/about_tab.py` — spacing fix for the top action row (SP1 L3).
- `packages/menubar/fulcra_menubar/preferences/window.py` — tab registration. Order: Plugins / Annotations / Notifications / About.

## Memory entries worth knowing

- `feedback_account_switch_caches.md` — every plugin caching def_id/tag_id has the same orphan-ingest hazard. The SP2 favorites Critical fix is the same pattern.
- `reference_fulcra_create_def_schema.md` — POST /user/v1alpha1/annotation needs `tags: []` + `description: ""` even on duration defs.
- `reference_fulcra_api.md` — canonical Fulcra URLs.
- `project_fulcra_menubar_running.md` — the menubar process I started this session (PID 34021 at handoff time).

## Architecture lessons carried forward + this session

1. **Alpine ↔ Lit reactivity gap.** Alpine's `x-effect` only registers top-level identifier reads; iteration helpers like `Object.values($data)` don't track. Bridge: install `Alpine.effect` from inside the wizard's `init()` (where `this` is the proxy) that dispatches a CustomEvent the Lit dispatcher listens for. Plus `hasChanged: () => true` on Lit prop declarations so identity-equal reassignments still re-render.
2. **Required-field-no-default is the right call for explicit metadata.** SP3's `collect_mode` could have defaulted to `kind→mode` but Attention is the special case (kind="manual" → collect_mode="live_continuous") and a silent wrong default would route it into the wrong popover section. Better to force every plugin author to declare.
3. **HTTP routes delegating to Daemon methods.** SP2's `_delete_definition` lives on the Daemon; the HTTP route is a thin wrapper that translates the `code` field back to HTTPException status. Same pattern would work for any other action that needs both HTTP + UDS surfaces.
4. **`Cache-Control: no-cache` on static assets.** FastAPI's `StaticFiles` defaults to ETag-only revalidation, which Chrome happily skips. Forcing `no-cache` keeps frontend dev iteration sane.
5. **Document the deep-link contract in both READMEs.** SP4 introduced URL-param routing; both consumer (web UI) and producer (menubar) READMEs now reference each other.

## Push state

Nothing pushed to `origin` this session. User reviews before push. When ready:

```bash
git push origin session/2026-05-26-account-switch-fixes-and-qa
```

Pre-push global rules (orphan/obsolete review, secret-leak scan) were executed per-sub-project during the session — but the per-sub-project sweeps don't substitute for a final whole-branch sweep before the actual push. Re-running them at push time is cheap (`git diff 1a310f6..HEAD` is the surface) and worth it.

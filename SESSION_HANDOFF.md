# Fulcra Collect — Session Handoff (2026-05-26)

Pick up where we left off. This file is the briefing for the next Claude session.

## Folder

```
/Users/Scanning/Developer/fulcra-tools
```

## State at handoff

- **Branch:** `main`, working tree dirty (substantial changes across `packages/collect/`, `packages/media-helpers/`, `packages/attention/`, `packages/fulcra-common/`, `packages/web-ui/dist/`, plus 3 new helper files and many test additions).
- **Nothing committed, nothing pushed.** Review before pushing.
- **Daemon:** stable port 9292. Start with `uv run fulcra-collect daemon` from the repo root. URL is mirrored to `~/.config/fulcra-collect/web-url`. **SO_REUSEPORT is now set so restart-immediate works — no more 60-90s TIME_WAIT pain.**
- **Tests:** ~980 pass across the workspace (`packages/collect/tests`, `packages/media-helpers/tests`, `packages/attention/tests`, `packages/dayone/tests`, `packages/fulcra-common/tests`). 61 pre-existing errors all from missing `pytest-mock` in the venv — unrelated.
- **Extension:** built at `packages/attention/chrome/dist/`, paired in the user's testing browser. Re-paired via wizard this session; pairing state persists across daemon restarts via `chrome.storage.local`.
- **Backup of pre-clear state:** `/tmp/fulcra-state-backup-20260525-213854/` (made before Q1 cleared plugin state for the re-QA).

## What landed THIS session (2026-05-26)

A lot. Two distinct phases.

### Phase 1 — Account-switch hazard generalisation + 5 "F" followups

Real bug at the root: every plugin caching `definition_id` in state files trusts the cache across daemon re-auths to a different Fulcra account. Events ingest into orphan def IDs that don't exist in the new account → invisible in the timeline despite returning HTTP 200 from `/ingest/v1/record/batch`.

- **F1** — soft-deleted 2 of my diagnostic-orphan defs (`8a6ba785`, `78fa66a9`) from the prior session. Kept `b331bb73` (real Attention) and `aa089284` (real Listened) since they're now bound to actual plugin state.
- **F2** — `SO_REUSEPORT` + `SO_REUSEADDR` on the daemon's port probe ([web.py:1385](packages/collect/fulcra_collect/web.py:1385)). Restart-immediate now works on Darwin; the 60-90s TIME_WAIT pain is gone.
- **F3** — tag-id cache parity: `_ensure_media_def` clears `media_state.tag_ids` whenever it detects a stale `*_definition_id` ([collect_plugins.py:42](packages/media-helpers/fulcra_media/collect_plugins.py:42)). Same hazard pattern as F4/F5, lower stakes.
- **F4** — activity-feed parity: `ctx.ensure_definition` emits an annotation event when a stale cache triggers re-resolution ([plugin.py:303](packages/collect/fulcra_collect/plugin.py:303)). Mirrors the surface added for the attention extension route.
- **F5** — daemon startup pre-flight ([daemon.py:117](packages/collect/fulcra_collect/daemon.py:117)). On boot, SHA-256 fingerprint of bearer-token is compared to a persisted value at `~/.config/fulcra-collect/auth-fingerprint`. Mismatch → invalidate cached def_ids + tag_ids across every per-plugin AND per-package state file, surface a "Account change detected" entry in the dashboard. **5 new tests** in `test_daemon.py`.

### Phase 2 — Cleared state + re-QA from scratch + 3 more bugs found and fixed

- **Q1** — backed up + cleared all per-plugin and per-package state. Restarted daemon (SO_REUSEPORT proved its worth).
- **Q2** — drove onboarding wizard fresh through the paired Chrome browser. Picker rendered cleanly with 5 plugins selected (Attention, Goodreads, Letterboxd, Generic RSS, Generic media CSV).
- **Q3** — walked Attention setup all the way through (intro → install extension → pair → definition_picker → done). One-click pair worked. **Found and fixed BUG #29**: `definition_picker` writes to per-plugin state but the extension reads from per-package state — the wizard's "Attention is set" was a lie until the user manually re-ran the plugin. Fixed via a fallback in `/api/extension/attention` that lazy-migrates per-plugin → per-package state on first POST + auto-calls `ensure_definitions` to seed the base tags. 1 new test covers the recovery. Live-verified after restart.
- **Q4** — Generic RSS pointed at https://news.ycombinator.com/rss. Run-now ingested 30 events; 23 visible in Fulcra. Pipeline fully verified for the RSS shape.
- **Q5** — skipped (RSS-shaped, covered by Q4).
- **Q6** — Generic CSV with synthetic 3-row file. Ran cleanly after a category fix. **Found BUG #31**: wizard offers `category=read` but importer only accepts `watched`/`listened`.

### New tasks filed during this session

- **#26** Markdown fenced code blocks render as inline backticks in the attention wizard step 2.
- **#27** Attention wizard step 2 install copy is dev-only (`cd packages/attention/chrome && npm install`) — fine for alpha, brittle for end-users.
- **#28** `definition_picker` filters by `annotation_type` only — Attention plugin offers "Listened" as a candidate (wrong canonical name).
- **#30** Attention events render as `0 h 0 m total` + invisible markers in the context.fulcradynamics.com timeline despite being in Fulcra. **Different codebase from this repo.**
- **#31** Generic CSV plugin schema/impl mismatch on `category` enum.

All filed in the in-session task list. Use `TaskList` to inspect.

## Resume command (paste into a fresh Claude session)

```
You are picking up a fulcra-tools QA + dev session. Read /Users/Scanning/Developer/fulcra-tools/SESSION_HANDOFF.md (yesterday's handoff) and CREDS_NEEDED.md (status of every plugin's verification).

Working directory: /Users/Scanning/Developer/fulcra-tools

Yesterday's session left a long working-tree of unstaged changes spanning collect/, media-helpers/, attention/, fulcra-common/, web-ui/. 980+ tests pass per-package. Nothing committed, nothing pushed — the user reviews before pushing.

Priority order for THIS session:
1. Review the dirty working tree with the user — especially the 5 "F" followups (F1-F5) + 3 "BUG" fixes (#29, #31) and the test additions. Resolve any feedback.
2. Walk pending tasks #26 (markdown fences), #27 (Attention install UX), #28 (definition_picker filter), #31 (Generic CSV category enum) — small fixes worth landing.
3. If user is around: ask them to spend 30s browsing in the paired Chrome browser so we can finally verify the chrome extension's live data path (vs the synthetic POSTs we used yesterday).
4. Defer #30 (timeline render bug) — it's in a different repo (context.fulcradynamics.com).

Constraint: do NOT commit or push. Working tree is dirty intentionally.
```

## Key files

- **Daemon HTTP:** `packages/collect/fulcra_collect/web.py`
- **Daemon core (incl. pre-flight + activity throttle):** `packages/collect/fulcra_collect/daemon.py`
- **Plugin contract + RunContext.{resolved_definition_id, ensure_definition}:** `packages/collect/fulcra_collect/plugin.py`
- **Plugin definitions:**
  - Most plugins: `packages/media-helpers/fulcra_media/collect_plugins.py` (uses the new `_ensure_media_def` helper)
  - Attention: `packages/attention/fulcra_attention/collect_plugin.py`
  - Day One: `packages/dayone/fulcra_dayone/collect_plugin.py` (no cache hazard — re-queries by name on every run)
- **Plugin health checks:**
  - Last.fm: `packages/media-helpers/fulcra_media/lastfm_health.py`
  - Trakt: `packages/media-helpers/fulcra_media/trakt_health.py`
- **Wizard frontend:** `packages/web-ui/dist/static/{app,dashboard,onboarding,wizard}.js` + `packages/web-ui/dist/index.html` + `packages/web-ui/dist/static/pair.html`
- **Browser extension:** `packages/attention/chrome/` (Vite/React; `npm run build` outputs to `dist/`)
- **Worker adapter (incl. new `definition_exists` + `create_definition` defaults):** `packages/collect/fulcra_collect/worker.py`
- **Shared Fulcra API client (new `definition_exists`):** `packages/fulcra-common/fulcra_common/client.py`
- **Data-source pathway reference (every plugin, every onboarding path):** `docs/how-do-i-get-my-data.md`

## Memory entries worth knowing

- `project_fulcra_tools_dev.md` — daemon invocations, menubar icon shape, expected dev-mode warnings.
- `feedback_alpine_xdata_remount.md` — Alpine 3 gotcha (re-init by toggling through null).
- `reference_fulcra_api.md` — canonical Fulcra URLs. The annotation endpoint is `/user/v1alpha1/annotation` (singular); event endpoints are `/data/v1alpha1/event/{DataType}`; ingest is `/ingest/v1/record/batch` with JSONL.
- `feedback_security_patterns.md` — secret-scrub patterns.

## Architecture lessons from this session

1. **State caches survive auth changes; APIs do not.** Fulcra's ingest accepts any source_id without validating def-existence. So a daemon re-auth leaves you in a quiet-failure state for every plugin that caches def_ids. The fix is multi-layer: per-call validation at the choke point (`ctx.resolved_definition_id`), per-package callers using `ctx.ensure_definition`, and proactive daemon-startup invalidation when the bearer-token changes.
2. **Per-plugin and per-package state files must stay in sync.** The wizard's `definition_picker` step writes per-plugin state, but plugins like Attention read from per-package state. Without a fallback or sync mechanism, the wizard appears successful while runtime fails. Fixed via lazy-migration in the extension route (#29) — but the underlying design issue (two-store data-model with no canonical source of truth) deserves a refactor later.
3. **Fulcra schema drift.** `create_definition` started requiring `tags: []` and `description: ""`, both of which the wire-helper paths supplied but the generic resolver did not. Defaults injected at the adapter layer ([worker.py:55](packages/collect/fulcra_collect/worker.py:55)). Worth setting up a contract test against the live API so the next drift surfaces immediately.
4. **The dashboard activity feed is the single most useful debugging surface.** Failures invisible in the timeline UI are immediately visible there. The runner's "Run failed: <reason>" entries + the worker's annotation events + the attention throttled events + the def-re-resolved entries together tell the user (and me) what's happening without grepping state files on disk.

## Don't push

The user reviews before pushing. Working tree is dirty intentionally.

# Onboarding "collect_modes" Screen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `collect_modes` phase between `signin` and `pick_plugins` in the onboarding state machine, presenting a single static screen that explains the historical-vs-live capture distinction, shows four worked combination examples, gives an Attention-extension callout, and encourages users to write their own plugins.

**Architecture:** Alpine-on-the-frontend state machine (in `packages/web-ui/dist/static/onboarding.js`) gains one new phase value `"collect_modes"`. The two existing transitions out of `signin` (CLI flow + paste-token flow) set `phase = "collect_modes"` instead of `phase = "pick_plugins"`; `_loadPlugins()` still fires async so the plugins list is ready by the time the user advances. A new `<template x-if="phase === 'collect_modes'">` block in `packages/web-ui/dist/index.html` renders the screen. Two new methods (`goToPickPlugins()` and `backToSignin()`) handle navigation. A tiny daemon-side change adds `Cache-Control: no-cache` to static-asset responses so this and future frontend edits don't need `?v=` query-string bumps to be visible to a returning browser.

**Tech Stack:** Vanilla JS + Alpine.js 3 (frontend); FastAPI + Starlette (daemon, only for the static-asset cache-headers fix).

**Source spec:** `docs/plans/2026-05-27-onboarding-collect-modes-screen.md` (committed at `eab6202`).

**Reading list before starting:**
- `docs/plans/2026-05-27-onboarding-collect-modes-screen.md` — the spec this plan implements.
- `packages/web-ui/dist/static/onboarding.js` — particularly the `phase` field at line 28 and the two signin-success transitions at lines 127 / 154.
- `packages/web-ui/dist/index.html` — particularly the `phase === 'signin'` template starting at line 125 and the `phase === 'pick_plugins'` template at line 213 (where the new block lands between them).
- `packages/collect/fulcra_collect/web.py:198-200` — the StaticFiles mount we extend in Task 1.

---

## File Structure

| File | Change | Responsibility after this plan |
|---|---|---|
| `packages/collect/fulcra_collect/web.py` | Modify | Mount `/static/` with a small wrapper that injects `Cache-Control: no-cache` on every response so the browser always revalidates. |
| `packages/web-ui/dist/static/onboarding.js` | Modify | Owns the `phase` state machine; gains the `"collect_modes"` value and three new methods (`goToCollectModes`, `goToPickPlugins`, `backToSignin`). |
| `packages/web-ui/dist/index.html` | Modify | Gains the static `phase === 'collect_modes'` template block. |
| `packages/collect/tests/test_routes_static.py` | Create | Tiny pytest that asserts the daemon serves `/static/wizard.js` with `Cache-Control: no-cache`. Future-proofs Task 1 against regression. |

Total surface: ~120 lines added (mostly the static HTML for the new screen), ~15 lines modified.

---

## Task 1: Daemon serves static assets with `Cache-Control: no-cache`

**Why first:** Without this, every subsequent change in this plan needs a manual hard-reload or a version-querystring bump to be visible in an already-open browser tab. Fixing it once eliminates the friction for the rest of the work and for every future frontend change.

**Files:**
- Modify: `packages/collect/fulcra_collect/web.py:198-200`
- Create: `packages/collect/tests/test_routes_static.py`

- [ ] **Step 1: Write the failing test.**

Create `packages/collect/tests/test_routes_static.py`:

```python
"""Static-asset serving — confirms cache headers force revalidation.

Why this test exists: FastAPI's StaticFiles defaults emit ETag and
last-modified but no Cache-Control, so Chrome happily serves the
cached body on conditional GETs even after the disk file changes. We
shipped a few sessions where frontend edits silently weren't visible
to a returning browser until the user did a hard reload. Adding
Cache-Control: no-cache makes the browser revalidate on every request.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_static_asset_has_no_cache_header(web_app_client: TestClient) -> None:
    # web_app_client fixture must already exist for the other route tests
    # in this suite; if it doesn't, look at how the sibling test files
    # build a TestClient and copy that fixture. We don't authenticate the
    # /static/ path — it's served before the bearer-token middleware.
    response = web_app_client.get("/static/wizard.js")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-cache"
```

Check whether the `web_app_client` fixture already exists in the test suite:

```bash
grep -rn "web_app_client\|TestClient" packages/collect/tests/ | head -20
```

If it doesn't exist, look at how `test_web.py` instantiates its TestClient and copy the exact pattern into a new `conftest.py` next to the test or inline the construction at the top of `test_routes_static.py`.

- [ ] **Step 2: Run the test to confirm it fails.**

```bash
cd packages/collect && uv run pytest tests/test_routes_static.py -v
```

Expected: FAIL — either with `AssertionError: None != 'no-cache'` (because StaticFiles doesn't set the header today) or `fixture 'web_app_client' not found`. If the latter, build the fixture in Step 1 and re-run.

- [ ] **Step 3: Implement the cache-header injector.**

Edit `packages/collect/fulcra_collect/web.py`. Find the lines around 198-200:

```python
    static_dir = _frontend_dir() / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
```

Replace with:

```python
    static_dir = _frontend_dir() / "static"
    if static_dir.exists():
        # StaticFiles defaults: ETag + last-modified, no Cache-Control.
        # Chrome serves the cached body on conditional GETs even when
        # the disk file changes, so frontend edits silently don't reach
        # an already-open tab until a hard reload. Wrap the mount in a
        # tiny ASGI middleware that forces revalidation. The 304 path
        # still works — browsers honour ETag with Cache-Control: no-cache,
        # they just always make the round-trip.
        _static_app = StaticFiles(directory=str(static_dir))

        async def _no_cache_static(scope, receive, send):
            async def _send_with_no_cache(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    # ASGI headers are a list of (bytes, bytes) tuples.
                    # Drop any pre-existing cache-control then append ours.
                    headers = [
                        (k, v) for (k, v) in headers
                        if k.lower() != b"cache-control"
                    ]
                    headers.append((b"cache-control", b"no-cache"))
                    message = {**message, "headers": headers}
                await send(message)
            await _static_app(scope, receive, _send_with_no_cache)

        app.mount("/static", _no_cache_static, name="static")
```

- [ ] **Step 4: Run the test to confirm it passes.**

```bash
cd packages/collect && uv run pytest tests/test_routes_static.py -v
```

Expected: PASS.

- [ ] **Step 5: Run the full collect test sweep to confirm no regression.**

```bash
cd packages/collect && uv run pytest -q
```

Expected: previous-baseline + 1 new test pass, 0 fail. (Baseline at session HEAD `fd969ca` is around 360 tests passing in the collect package.)

- [ ] **Step 6: Commit.**

```bash
git add packages/collect/fulcra_collect/web.py packages/collect/tests/test_routes_static.py
git commit -m "fix(collect): serve /static/* with Cache-Control: no-cache

FastAPI's StaticFiles default emits ETag and last-modified but no
Cache-Control, so Chrome serves the cached body on conditional GETs
even when the disk file changes. Frontend edits silently fail to
reach an already-open browser tab until the user does a hard reload.

Wrap the static mount in a tiny ASGI middleware that injects
Cache-Control: no-cache on every response. ETag-based revalidation
still works — browsers will still get 304s when nothing changed —
but they'll always make the round-trip, so file changes show up
without a hard reload.

Discovered 2026-05-27 while testing the refactor #68 Lit components:
each iteration on the dispatcher's reactivity bridge required a
querystring-bumped script tag to be visible. Fixing the cache header
once removes that friction permanently."
```

---

## Task 2: Add `collect_modes` to the onboarding state machine

**Files:**
- Modify: `packages/web-ui/dist/static/onboarding.js` — the phase comment on line 27, and the two phase transitions on lines 127 and 154.

- [ ] **Step 1: Update the phase-list comment.**

Open `packages/web-ui/dist/static/onboarding.js`. Find lines 7-16 (the multi-step flow comment):

```javascript
 * Multi-step flow:
 *   0  welcome         — static intro, Next button
 *   1  signin          — sign in to Fulcra. Default: browser-based device-auth
 *                        flow via POST /api/fulcra/auth/cli_login. Fallback
 *                        (when fulcra CLI is unavailable, or user clicks
 *                        "Use a token instead"): paste-token via POST
 *                        /api/fulcra/auth/token.
 *   2  pick_plugins    — grouped plugin checkboxes from GET /api/status
 *   3  configure       — walks each picked plugin's setup_steps via createWizard()
 *   4  done            — summary, links to dashboard
```

Replace with:

```javascript
 * Multi-step flow:
 *   0  welcome         — static intro, Next button
 *   1  signin          — sign in to Fulcra. Default: browser-based device-auth
 *                        flow via POST /api/fulcra/auth/cli_login. Fallback
 *                        (when fulcra CLI is unavailable, or user clicks
 *                        "Use a token instead"): paste-token via POST
 *                        /api/fulcra/auth/token.
 *   2  collect_modes   — static explanation of historical-vs-live capture
 *                        with four worked combo examples (music, TV,
 *                        podcasts, Apple movies), an Attention callout,
 *                        and a closing encouragement to write your own
 *                        plugin. No API calls, no per-plugin state.
 *   3  pick_plugins    — grouped plugin checkboxes from GET /api/status.
 *                        _loadPlugins() fires async during collect_modes
 *                        so the list is ready by the time the user
 *                        advances; the existing pickLoading flag handles
 *                        the unlikely race where the user clicks Next
 *                        before the request returns.
 *   4  configure       — walks each picked plugin's setup_steps via createWizard()
 *   5  done            — summary, links to dashboard
```

- [ ] **Step 2: Update the `phase` initial-value comment.**

Find line 27 (currently a `// Current high-level phase: ...` comment) and the field declaration on line 28:

```javascript
    // Current high-level phase: welcome | signin | pick_plugins | configure | done
    phase: "welcome",
```

Replace with:

```javascript
    // Current high-level phase: welcome | signin | collect_modes | pick_plugins | configure | done
    phase: "welcome",
```

- [ ] **Step 3: Redirect the CLI-signin success transition through `collect_modes`.**

Find lines 126-129 (inside `signinViaCli`):

```javascript
        if (result.ok) {
          this.phase = "pick_plugins";
          await this._loadPlugins();
        } else {
```

Replace with:

```javascript
        if (result.ok) {
          // Hand off to the static explainer screen; load plugins in the
          // background so the pick_plugins list is ready by the time the
          // user advances. pickLoading covers the race if they're faster
          // than the API.
          this.phase = "collect_modes";
          this._loadPlugins();  // intentionally not awaited
        } else {
```

- [ ] **Step 4: Redirect the paste-token success transition through `collect_modes`.**

Find lines 153-156 (inside `submitToken`):

```javascript
        if (result.ok) {
          this.phase = "pick_plugins";
          await this._loadPlugins();
        } else {
```

Replace with:

```javascript
        if (result.ok) {
          // Same flow as signinViaCli — explainer then pick.
          this.phase = "collect_modes";
          this._loadPlugins();  // intentionally not awaited
        } else {
```

- [ ] **Step 5: Add the two navigation methods.**

Find a method on the returned object near the other phase transitions — `signinViaCli` ends around line 137, `submitToken` around line 164. Add the two new methods immediately after `submitToken`'s closing brace, before `_loadPlugins`:

```javascript
    // collect_modes → pick_plugins (Next button on the static explainer screen).
    goToPickPlugins() {
      this.phase = "pick_plugins";
      // _loadPlugins() was fired during signin success; only re-load
      // if it never completed (e.g., the user is on a slow connection
      // and clicked Next during the request). pickLoading flips to
      // false in _loadPlugins's finally block, so this check is safe.
      if (this.pickLoading) {
        // already in flight from signin handler; nothing to do
        return;
      }
      if (this.allPlugins.length === 0) {
        this._loadPlugins();
      }
    },

    // collect_modes → signin (Back button on the static explainer screen).
    backToSignin() {
      this.phase = "signin";
    },
```

- [ ] **Step 6: Verify JS syntax.**

```bash
node --check packages/web-ui/dist/static/onboarding.js
```

Expected: exit 0, no output.

- [ ] **Step 7: Commit.**

```bash
git add packages/web-ui/dist/static/onboarding.js
git commit -m "feat(web-ui): add collect_modes phase to onboarding state machine

Inserts a new \"collect_modes\" phase between signin and pick_plugins.
Both signin paths (CLI and paste-token) now set phase = 'collect_modes'
on success instead of pick_plugins. _loadPlugins() fires in the
background (not awaited) so the picker list is ready by the time the
user advances; the existing pickLoading flag covers the race window.

Adds two navigation methods: goToPickPlugins (Next) and backToSignin
(Back). The template block that renders the actual screen lands in
the next commit.

Refs onboarding-collect-modes spec."
```

---

## Task 3: Add the `collect_modes` template block to `index.html`

**Files:**
- Modify: `packages/web-ui/dist/index.html` — insert a new template block between the existing `phase === 'signin'` close tag (line ~210) and the `phase === 'pick_plugins'` opening tag (line 213).

- [ ] **Step 1: Locate the insertion point.**

Open `packages/web-ui/dist/index.html`. Find the lines around 210-213:

```html
            </div>
          </div>
        </template>

        <!-- Phase: pick_plugins -->
        <template x-if="phase === 'pick_plugins'">
```

The new block goes immediately after the closing `</template>` (around line 210) and before the `<!-- Phase: pick_plugins -->` comment.

- [ ] **Step 2: Insert the new template block.**

Paste this block immediately before the `<!-- Phase: pick_plugins -->` comment:

```html
        <!-- Phase: collect_modes — static explainer screen between signin
             and pick_plugins. Single-screen content; no API calls. See
             docs/plans/2026-05-27-onboarding-collect-modes-screen.md for
             the source spec. -->
        <template x-if="phase === 'collect_modes'">
          <div class="space-y-6">
            <div>
              <h2 class="text-2xl font-semibold mb-2">How to gather and update your data with Fulcra Collect</h2>
              <div class="space-y-3 text-slate-600 text-sm">
                <p>
                  Some plugins import a one-time export — your
                  <span class="font-medium text-slate-800">historical</span>
                  data. Others capture new events as they happen — your
                  <span class="font-medium text-slate-800">live</span>
                  data. They're safe to mix: when sources overlap, Fulcra
                  deduplicates them so you don't get double counts.
                </p>
                <p>
                  Here are some examples of how historical and live sources
                  fit together.
                </p>
              </div>
            </div>

            <!-- 2×2 combo grid. md: breakpoint flips to two columns;
                 below that it stacks vertically. -->
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <!-- Music -->
              <div class="border border-slate-200 rounded-lg p-4 space-y-2">
                <div class="flex items-center gap-2">
                  <span class="text-xl" aria-hidden="true">🎵</span>
                  <span class="font-semibold text-sm">Music</span>
                </div>
                <div class="text-xs">
                  <div class="text-slate-400 uppercase tracking-wider">Live</div>
                  <div class="text-slate-700">Last.fm — scrobbles every play, going forward</div>
                </div>
                <div class="text-xs">
                  <div class="text-slate-400 uppercase tracking-wider">Historical</div>
                  <div class="text-slate-700">Apple Music takeout — years of past listens from your account export</div>
                </div>
                <div class="text-xs text-emerald-700 font-medium pt-1">
                  → one unified <span class="font-semibold">Listened</span> track
                </div>
              </div>

              <!-- TV & Movies -->
              <div class="border border-slate-200 rounded-lg p-4 space-y-2">
                <div class="flex items-center gap-2">
                  <span class="text-xl" aria-hidden="true">🎬</span>
                  <span class="font-semibold text-sm">TV &amp; Movies</span>
                </div>
                <div class="text-xs">
                  <div class="text-slate-400 uppercase tracking-wider">Live</div>
                  <div class="text-slate-700">Trakt — scrobbles new watches in real time</div>
                </div>
                <div class="text-xs">
                  <div class="text-slate-400 uppercase tracking-wider">Historical</div>
                  <div class="text-slate-700">Netflix takeout — your full Netflix watch history</div>
                </div>
                <div class="text-xs text-emerald-700 font-medium pt-1">
                  → one unified <span class="font-semibold">Watched</span> track
                </div>
              </div>

              <!-- Podcasts -->
              <div class="border border-slate-200 rounded-lg p-4 space-y-2">
                <div class="flex items-center gap-2">
                  <span class="text-xl" aria-hidden="true">🎙️</span>
                  <span class="font-semibold text-sm">Podcasts</span>
                </div>
                <div class="text-xs">
                  <div class="text-slate-400 uppercase tracking-wider">Live</div>
                  <div class="text-slate-700">Apple Podcasts (on-device) — polls the local Podcasts database every 6 hours</div>
                </div>
                <div class="text-xs">
                  <div class="text-slate-400 uppercase tracking-wider">Historical</div>
                  <div class="text-slate-700">Apple Podcasts (Time Machine recovery) — pulls episodes from older Time Machine snapshots of the same database</div>
                </div>
                <div class="text-xs text-emerald-700 font-medium pt-1">
                  → one unified <span class="font-semibold">Listened</span> track
                </div>
              </div>

              <!-- Movies via Apple -->
              <div class="border border-slate-200 rounded-lg p-4 space-y-2">
                <div class="flex items-center gap-2">
                  <span class="text-xl" aria-hidden="true">🍿</span>
                  <span class="font-semibold text-sm">Movies via Apple</span>
                </div>
                <div class="text-xs">
                  <div class="text-slate-400 uppercase tracking-wider">Live</div>
                  <div class="text-slate-700">Trakt — scrobbles new watches in real time</div>
                </div>
                <div class="text-xs">
                  <div class="text-slate-400 uppercase tracking-wider">Historical</div>
                  <div class="text-slate-700">Apple takeout — your iTunes / Apple TV purchase and rental history</div>
                </div>
                <div class="text-xs text-emerald-700 font-medium pt-1">
                  → one unified <span class="font-semibold">Watched</span> track
                </div>
              </div>
            </div>

            <!-- Attention callout — its model is genuinely different from
                 the four pair-shaped categories above, so it gets its
                 own distinct visual treatment. -->
            <div class="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700">
              <span class="font-semibold">🌐 A special case: Attention.</span>
              The Fulcra Attention browser extension captures live tab
              activity as you browse — and can backfill from your
              existing browser history when you install it. You can
              pair the same Attention track from multiple browsers
              across multiple machines (Arc, Chrome, work laptop,
              home), and every paired instance feeds the same unified
              track.
            </div>

            <!-- Closing encouragement: extensibility. -->
            <div class="text-sm text-slate-600 space-y-2">
              <p>
                <span class="font-semibold text-slate-800">Don't see what you need?</span>
                Fulcra Collect is open and extensible — every plugin we
                ship implements a documented contract. You can write
                your own plugin to capture whatever data matters to your
                future self, whether that's something we haven't built
                yet or something only you have.
              </p>
              <p class="text-xs text-slate-500">
                These are just examples — every plugin works on its own
                too, and most categories have additional sources we
                didn't show here.
              </p>
            </div>

            <!-- Navigation. Matches the existing onboarding back/next pattern. -->
            <div class="flex gap-3 pt-2">
              <button @click="goToPickPlugins()"
                      class="px-6 py-2.5 rounded bg-violet-600 text-white font-medium hover:bg-violet-700 transition-colors">
                Next
              </button>
              <button @click="backToSignin()"
                      class="px-4 py-2 rounded border border-slate-300 text-slate-600 hover:bg-slate-50 text-sm">
                Back
              </button>
            </div>
          </div>
        </template>
```

- [ ] **Step 3: Verify HTML well-formedness with a quick sanity grep.**

```bash
# Count opening vs closing template tags inside the onboarding section.
# The numbers should still match after the insertion.
grep -c "<template" packages/web-ui/dist/index.html
grep -c "</template>" packages/web-ui/dist/index.html
```

Expected: both numbers are equal. (Take note of the count before this step; it should grow by exactly 1 on each side after Step 2.)

- [ ] **Step 4: Commit.**

```bash
git add packages/web-ui/dist/index.html
git commit -m "feat(web-ui): add collect_modes screen between signin and pick_plugins

Static explainer screen rendered when phase === 'collect_modes'. Lays
out the historical-vs-live framing, four worked combo examples
(Last.fm + Apple Music takeout, Trakt + Netflix takeout, Apple Podcasts
on-device + Time Machine recovery, Trakt + Apple takeout), an
Attention-extension callout (live + browser-history backfill +
multi-machine pairing), and a closing encouragement to write custom
plugins.

Pure markup — Alpine bindings go to goToPickPlugins / backToSignin
methods that landed in the previous commit. No API calls, no
per-plugin state. The 2×2 grid collapses to a single column at the
md: breakpoint.

Refs onboarding-collect-modes spec."
```

---

## Task 4: End-to-end manual verification

**Files:** none modified — pure verification + final commit if anything trailing needs cleanup.

- [ ] **Step 1: Restart the daemon to pick up the Task 1 web.py change.**

```bash
launchctl kickstart -k gui/$(id -u)/com.fulcra.collect
sleep 3
TOKEN=$(cat ~/.config/fulcra-collect/web-token)
curl -s -o /dev/null -w "HTTP %{http_code}\n" -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:9292/api/version
```

Expected: `HTTP 200`. (If 404 or refused, wait a few more seconds and retry; launchd's `KeepAlive=true` will respawn.)

- [ ] **Step 2: Verify the cache-header fix.**

```bash
curl -sI http://127.0.0.1:9292/static/wizard.js | grep -i cache-control
```

Expected: `cache-control: no-cache`.

- [ ] **Step 3: Walk the onboarding flow in a browser.**

Open `http://127.0.0.1:9292/?onboarding=1` (or whatever the existing dashboard's "Re-run onboarding wizard" button uses — check `dashboard.js` for the exact entry-point if unsure). Walk:

1. **welcome screen** — Next.
2. **signin screen** — sign in (CLI or paste-token, whichever's available). Expected: lands on the new `collect_modes` screen, NOT directly on `pick_plugins`.
3. **collect_modes screen** — confirm:
   - Title reads "How to gather and update your data with Fulcra Collect".
   - Lede paragraphs render with the bold "historical" / "live" terms.
   - All four combo cards visible. At desktop width (≥ ~1024 px) they're in a 2×2 grid; resize the window narrow (~480 px) and confirm they stack to single column.
   - Each card has icon + category name + Live block + Historical block + green "→ one unified … track" line.
   - Attention callout renders with the 🌐 emoji and slate-50 background.
   - Closing "Don't see what you need?" paragraph + small "These are just examples" footnote.
   - Back button returns to signin.
   - Next button advances to pick_plugins. The picker list is already loaded (no visible loading spinner).
4. **pick_plugins screen** — confirm it still works exactly as before (no regressions to existing behaviour).
5. **Cancel out** of the wizard (Skip Onboarding link in the header) to return to the dashboard.

- [ ] **Step 4: Run the full Python test sweep.**

```bash
uv run --all-packages pytest -q packages/
```

Expected: 1444 passed (the prior 1443 baseline + 1 new test from Task 1), 1 skipped.

- [ ] **Step 5: Run `node --check` over the static JS to catch any syntax regression.**

```bash
for f in packages/web-ui/dist/static/{onboarding,wizard,dashboard,settings,app}.js \
         packages/web-ui/dist/static/components/*.js; do
  echo "checking $f"
  node --check "$f" || exit 1
done
```

Expected: every file prints its name and exits 0.

- [ ] **Step 6: Pre-push orphan / obsolete review.**

Per `~/.claude/CLAUDE.md`, before any push: skim the staged diff for orphan code, stale comments, unused imports introduced by the change, half-finished features. For this plan the surface is small — the only realistic finding would be a stale comment in `onboarding.js` referring to the old phase order. Re-check the multi-step-flow comment block at the top of the file is consistent with the actual phase list.

If you find anything, fix it in a follow-up commit:

```bash
git add packages/web-ui/dist/static/onboarding.js  # or whatever you touched
git commit -m "chore(web-ui): orphan/obsolete sweep after collect_modes (onboarding-collect-modes follow-up)"
```

- [ ] **Step 7: Final push (optional — depending on user preference).**

The user reviews session-branch state before merge. This plan stops at the local-commit boundary. If the user wants to push:

```bash
git push origin session/2026-05-26-account-switch-fixes-and-qa
```

(But don't push autonomously — wait for the user's go-ahead, per `~/.claude/CLAUDE.md`'s commit-or-push gating.)

---

## Acceptance Checklist

After all tasks land, all of these should be true:

- [ ] `cd packages/collect && uv run pytest -q` passes including `tests/test_routes_static.py`.
- [ ] `curl -sI http://127.0.0.1:9292/static/wizard.js | grep cache-control` returns `cache-control: no-cache`.
- [ ] `node --check` on every JS file in `packages/web-ui/dist/static/` exits 0.
- [ ] Onboarding walked end-to-end in a browser, landing on collect_modes between signin and pick_plugins.
- [ ] Both Back (→ signin) and Next (→ pick_plugins) buttons work on the new screen.
- [ ] Cards render as 2×2 at desktop width, single column at narrow width.
- [ ] All four combo cards plus the Attention callout plus the closing paragraph are present and match the spec's wording exactly.
- [ ] The dashboard's existing "Add plugin" / "Re-run onboarding wizard" paths into the flow still work — they bypass `collect_modes` (entering at `pick_plugins` directly) because they go through `boot()`'s `requestedPhase` path, which the plan does NOT touch.
- [ ] No regressions in the existing test suite (`uv run --all-packages pytest -q packages/`).

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Browser STILL serves cached `onboarding.js` because Service Worker is in the way. | Daemon doesn't register a Service Worker for `/static/`, but if a future change does, the cache-header fix won't cover it. Out of scope for this plan; document in a follow-up if a SW lands. |
| The 2×2 grid wraps awkwardly between md and lg breakpoints because card content lengths differ. | Tailwind's `grid` with `grid-cols-1 md:grid-cols-2` plus the explicit `space-y-2` inside each card produces equal-height rows on aligned cards. Verify visually in Task 4 Step 3 at multiple widths; if a card overflows, shorten the live/historical body strings (they're soft user-facing copy and easy to trim). |
| `_loadPlugins()` fails while the user is on `collect_modes` and `pickError` flashes only when they hit Next. | Existing behaviour: `pickError` renders inside the pick_plugins template. User sees the error on the next screen, which is no worse than today (where the same `await` failure would show on the signin → pick_plugins transition with the same error template). |
| User's daemon was started by launchd before Task 1's web.py change. | Task 4 Step 1 force-restarts the daemon via `launchctl kickstart -k`; that picks up the new code. If launchd isn't managing the daemon, kill the PID by hand (`ps aux \| grep fulcra-collect`) — KeepAlive will respawn it, or restart manually with `uv run --directory packages/collect fulcra-collect daemon` in a background shell. |

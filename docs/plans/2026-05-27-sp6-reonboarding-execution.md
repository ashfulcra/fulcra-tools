# SP6 — Re-onboarding pre-check + Reconfigure toggle: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Fresh subagent per task with two-stage review (spec-fit + code-quality).

**Goal:** Make re-running onboarding skip already-set-up plugins by default, with a per-row "Reconfigure" escape hatch.

**Architecture:** Web-UI only. Two state additions to the Alpine `onboarding()` data: `wasEnabledAtStart` (immutable snapshot of which plugins were enabled when the picker first loaded) and `reconfigureIds` (mutable opt-in set). Skip logic runs at `startConfigure()` time — skipped plugins never enter `pluginsToSetup` at all, so `_enterCurrentPlugin()` stays untouched. Three-bucket summary in the `done` phase.

**Tech Stack:** Alpine.js + Lit web components (the Lit side isn't touched by this work — `pick_plugins` and `done` are plain Alpine templates in `index.html`).

**Spec:** `docs/plans/2026-05-27-sp6-reonboarding-precheck-skip-design.md`

---

## File Structure

**Modified:**

- `packages/web-ui/dist/static/onboarding.js` — Alpine `onboarding()` data object. Adds two state properties, modifies `_loadPlugins()` / `togglePlugin()` / `startConfigure()`, adds `toggleReconfigure()` + `isReconfiguring()` helpers, adds three derived counts for the done summary.
- `packages/web-ui/dist/index.html` — `pick_plugins` template (sub-header copy, per-row Reconfigure toggle), `done` template (three-bucket summary).

**Created:** None.

**Tests:** None new. SP6 is a state-machine refinement on top of endpoints that already have pytest coverage; verification is the manual walkthrough at the bottom of the spec. YAGNI on a JS test harness for this change.

---

## Task 1: Seed `selectedIds` from enabled plugins; add `wasEnabledAtStart` and `reconfigureIds`

**Files:**

- Modify: `packages/web-ui/dist/static/onboarding.js` (data object + `_loadPlugins` + `togglePlugin`)

The picker's "what was already set up when I got here" memory has to live separately from `selectedIds` (which the user mutates). We snapshot `wasEnabledAtStart` exactly once per `_loadPlugins()` call so a refresh inside the same session updates the picture, but a click on a checkbox does not.

- [ ] **Step 1: Add the two new data properties**

In `packages/web-ui/dist/static/onboarding.js`, in the `onboarding()` return object's `pick_plugins state` block (around line 54-59), change from:

```js
    // --- pick_plugins state ---
    allPlugins: [],         // full list from /api/status
    categories: [],         // [{name, plugins: [{id, name, description, category, enabled}]}]
    selectedIds: new Set(), // ids the user checked
    pickLoading: true,
    pickError: "",
```

to:

```js
    // --- pick_plugins state ---
    allPlugins: [],         // full list from /api/status
    categories: [],         // [{name, plugins: [{id, name, description, category, enabled}]}]
    selectedIds: new Set(), // ids the user checked (mutable; seeded from wasEnabledAtStart on load)
    // Immutable snapshot of which plugins were already enabled when the
    // picker last loaded. Used to (a) decide which rows render with a
    // "Set up" pill + Reconfigure toggle and (b) skip the per-plugin
    // wizard walk for already-enabled plugins the user didn't flip
    // Reconfigure on. Refreshed only on _loadPlugins(), never on user
    // clicks — that's the whole point.
    wasEnabledAtStart: new Set(),
    // Opt-in set: plugin ids the user has flipped Reconfigure ON for.
    // Only plugins in wasEnabledAtStart can be in here; for new plugins
    // there's nothing to reconfigure. Cleared when un-checking the row
    // so toggling stays consistent.
    reconfigureIds: new Set(),
    pickLoading: true,
    pickError: "",
```

- [ ] **Step 2: Seed `selectedIds` + snapshot `wasEnabledAtStart` in `_loadPlugins`**

Find the `_loadPlugins` method (around line 203). After `this.allPlugins = status.plugins ?? [];` (around line 208), add:

```js
        // Pre-check every plugin the daemon reports as enabled, and snapshot
        // which ids were enabled at this moment so the configure walk can
        // skip past them by default. wasEnabledAtStart is immutable for the
        // rest of this trip; selectedIds is what the user mutates.
        const enabledIds = this.allPlugins
          .filter(p => p.enabled)
          .map(p => p.id);
        this.selectedIds = new Set(enabledIds);
        this.wasEnabledAtStart = new Set(enabledIds);
        this.reconfigureIds = new Set();
```

- [ ] **Step 3: Update `togglePlugin` to keep `reconfigureIds` consistent**

Find `togglePlugin(id)` (around line 247). Change from:

```js
    togglePlugin(id) {
      const next = new Set(this.selectedIds);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      this.selectedIds = next;
    },
```

to:

```js
    togglePlugin(id) {
      const next = new Set(this.selectedIds);
      if (next.has(id)) {
        next.delete(id);
        // Un-checking is canonical "not touching this trip" — drop any
        // Reconfigure opt-in too so re-checking starts from the safe
        // (skip) default.
        if (this.reconfigureIds.has(id)) {
          const nextRc = new Set(this.reconfigureIds);
          nextRc.delete(id);
          this.reconfigureIds = nextRc;
        }
      } else {
        next.add(id);
      }
      this.selectedIds = next;
    },
```

- [ ] **Step 4: Add `toggleReconfigure(id)` and `isReconfiguring(id)` helpers**

Right after the closing brace of `isSelected(id)` (around line 259), add:

```js
    // Flip the Reconfigure opt-in for a row. Only meaningful for plugins
    // in wasEnabledAtStart; for a new plugin, calling this is a no-op
    // because there's nothing to reconfigure.
    toggleReconfigure(id) {
      if (!this.wasEnabledAtStart.has(id)) return;
      const next = new Set(this.reconfigureIds);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      this.reconfigureIds = next;
    },

    isReconfiguring(id) {
      return this.reconfigureIds.has(id);
    },

    // Was this plugin already enabled when the picker first loaded?
    // Used by the template to decide whether to render the "Set up" pill
    // + Reconfigure toggle on the row.
    wasEnabledOnLoad(id) {
      return this.wasEnabledAtStart.has(id);
    },
```

- [ ] **Step 5: Hard-reload the dev daemon's web UI and smoke-test**

Open `http://127.0.0.1:9292/` in the browser. Sign in (or arrive already signed in). Trigger onboarding via "Run setup wizard" on the dashboard. Confirm:

- The picker loads.
- Currently-enabled plugins render with their checkbox checked.
- New plugins render with their checkbox unchecked.
- The Reconfigure toggle is NOT yet visible (Task 2 adds the UI).
- Un-checking and re-checking a pre-checked plugin works without error.
- Console has no errors.

- [ ] **Step 6: Commit**

```bash
git add packages/web-ui/dist/static/onboarding.js
git commit -m "feat(web-ui): seed onboarding picker from already-enabled plugins (SP6 task 1)

Re-running onboarding now pre-checks every plugin the daemon reports
as enabled, instead of forcing the user to re-tick every box. The
selection is stored in two parallel sets so the picker logic can
distinguish 'currently checked' (selectedIds — mutable) from 'was
already set up when I got here' (wasEnabledAtStart — immutable
snapshot from the most recent /api/status load).

Adds the reconfigureIds set + toggleReconfigure/isReconfiguring/
wasEnabledOnLoad helpers that Task 2's UI consumes. Empty for now —
no template wiring yet. togglePlugin also clears the matching
reconfigureIds entry on un-check so flipping back and forth never
strands the opt-in state.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Picker UI — sub-header copy, "Set up" pill, Reconfigure toggle

**Files:**

- Modify: `packages/web-ui/dist/index.html` (pick_plugins template, around line 361-418)

- [ ] **Step 1: Update the picker sub-header copy**

In `packages/web-ui/dist/index.html`, find the `pick_plugins` header block (around line 364-369). Change from:

```html
            <div>
              <h2 class="text-2xl font-semibold mb-2">Pick what to collect</h2>
              <p class="text-slate-600 text-sm">
                Choose the services you want to record. You can add more later.
              </p>
            </div>
```

to:

```html
            <div>
              <h2 class="text-2xl font-semibold mb-2">Pick what to collect</h2>
              <p class="text-slate-600 text-sm">
                Choose the services you want to record. Plugins already set up
                are pre-checked — un-check to skip them this trip, or flip
                <span class="font-medium">Reconfigure</span> on a row to walk
                through its settings again.
              </p>
            </div>
```

- [ ] **Step 2: Replace the "enabled" pill with a "Set up" pill + Reconfigure toggle**

Find the per-row template (around line 386-401). Change from:

```html
                      <template x-for="p in cat.plugins" :key="p.id">
                        <label class="flex items-start gap-3 px-4 py-3 hover:bg-slate-50 cursor-pointer border-b border-slate-100 last:border-0">
                          <input type="checkbox"
                                 :checked="isSelected(p.id)"
                                 @change="togglePlugin(p.id)"
                                 class="mt-0.5 h-4 w-4 rounded border-slate-300 text-violet-600 focus:ring-violet-500">
                          <div class="flex-1 min-w-0">
                            <div class="font-medium text-sm" x-text="p.name"></div>
                            <div class="text-xs text-slate-500 mt-0.5"
                                 x-text="p.description || ''"></div>
                          </div>
                          <template x-if="p.enabled">
                            <span class="text-xs text-violet-600 font-medium shrink-0 mt-0.5">enabled</span>
                          </template>
                        </label>
                      </template>
```

to:

```html
                      <template x-for="p in cat.plugins" :key="p.id">
                        <div class="border-b border-slate-100 last:border-0">
                          <label class="flex items-start gap-3 px-4 py-3 hover:bg-slate-50 cursor-pointer">
                            <input type="checkbox"
                                   :checked="isSelected(p.id)"
                                   @change="togglePlugin(p.id)"
                                   class="mt-0.5 h-4 w-4 rounded border-slate-300 text-violet-600 focus:ring-violet-500">
                            <div class="flex-1 min-w-0">
                              <div class="font-medium text-sm flex items-center gap-2">
                                <span x-text="p.name"></span>
                                <template x-if="wasEnabledOnLoad(p.id)">
                                  <span class="text-[10px] uppercase tracking-wide bg-emerald-50 text-emerald-700 border border-emerald-200 rounded px-1.5 py-0.5 font-medium">Set up</span>
                                </template>
                              </div>
                              <div class="text-xs text-slate-500 mt-0.5"
                                   x-text="p.description || ''"></div>
                            </div>
                          </label>
                          <!-- Reconfigure toggle: only for already-enabled
                               plugins that are still checked. Hidden when
                               un-checked (un-check = "not touching this
                               trip"; Reconfigure would be meaningless). -->
                          <template x-if="wasEnabledOnLoad(p.id) && isSelected(p.id)">
                            <div class="pl-11 pr-4 pb-3 -mt-1">
                              <label class="inline-flex items-center gap-2 text-xs text-slate-600 cursor-pointer">
                                <input type="checkbox"
                                       :checked="isReconfiguring(p.id)"
                                       @change="toggleReconfigure(p.id)"
                                       class="h-3.5 w-3.5 rounded border-slate-300 text-violet-600 focus:ring-violet-500">
                                <span>Reconfigure — walk through this plugin's setup again</span>
                              </label>
                            </div>
                          </template>
                        </div>
                      </template>
```

The wrapping `<div class="border-b ...">` replaces the bordered `<label>` because the Reconfigure toggle needs to sit outside the row's clickable label area (otherwise clicking it would also toggle the main checkbox). Border now lives on the wrapping div so the visual row separator is preserved.

- [ ] **Step 3: Tweak the primary button copy for the re-run zero-state**

Find the primary action button (around line 407-410). Change from:

```html
                  <button @click="startConfigure()"
                          class="px-6 py-2.5 rounded bg-violet-600 text-white font-medium hover:bg-violet-700 transition-colors">
                    <span x-text="selectedCount > 0 ? `Set up ${selectedCount} plugin${selectedCount !== 1 ? 's' : ''}` : 'Skip for now'"></span>
                  </button>
```

to:

```html
                  <button @click="startConfigure()"
                          class="px-6 py-2.5 rounded bg-violet-600 text-white font-medium hover:bg-violet-700 transition-colors">
                    <span x-text="primaryActionLabel"></span>
                  </button>
```

Then in `packages/web-ui/dist/static/onboarding.js`, add the `primaryActionLabel` getter right after `selectedCount` (around line 261-263). Add:

```js
    // Button copy varies by intent so the user knows what clicking it does:
    //   - nothing selected → "Skip for now" (same as before)
    //   - everything pre-checked, no Reconfigure → "Continue" (nothing to walk)
    //   - some plugins to walk → "Set up N plugin(s)" (same as before)
    get primaryActionLabel() {
      if (this.selectedIds.size === 0) return "Skip for now";
      const walkCount = [...this.selectedIds].filter(id =>
        !this.wasEnabledAtStart.has(id) || this.reconfigureIds.has(id)
      ).length;
      if (walkCount === 0) return "Continue";
      return `Set up ${walkCount} plugin${walkCount !== 1 ? "s" : ""}`;
    },
```

- [ ] **Step 4: Hard-reload and visual-smoke**

Open `http://127.0.0.1:9292/`, trigger onboarding. Confirm:

- Sub-header includes the "Plugins already set up are pre-checked…" sentence.
- Already-enabled plugin rows show the emerald "Set up" pill next to the plugin name.
- A Reconfigure toggle row appears under each pre-checked already-enabled plugin.
- Un-checking the row hides the Reconfigure toggle.
- Re-checking shows it again (with Reconfigure off — Task 1 cleared it on un-check).
- Primary button reads "Continue" when nothing needs walking (all pre-checked, none Reconfigure).
- Primary button reads "Set up N plugin(s)" when one is new or one has Reconfigure on.

- [ ] **Step 5: Commit**

```bash
git add packages/web-ui/dist/index.html packages/web-ui/dist/static/onboarding.js
git commit -m "feat(web-ui): Reconfigure toggle + 'Set up' pill in onboarding picker (SP6 task 2)

Re-onboarding now visually telegraphs which plugins are pre-checked
because they were already set up (emerald 'Set up' pill next to the
plugin name) and gives the user a per-row Reconfigure toggle to opt
back into the wizard walk when they want to rotate a credential or
change a definition. Picker sub-header copy explains the new model
in one sentence.

Primary action button copy switches to 'Continue' when every checked
plugin is already enabled and the user has not flipped Reconfigure
on any row — that's the common case for a casual re-run, and showing
'Set up 5 plugins' there would be misleading.

The Reconfigure toggle lives outside the row's clickable label so
clicks on it don't also flip the main checkbox; row border moved to
the wrapping div to preserve the visual separator.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Skip already-enabled plugins in `startConfigure` (whole-plugin skip)

**Files:**

- Modify: `packages/web-ui/dist/static/onboarding.js` (`startConfigure` + add `skippedIds` / `walkedCategories` for the done summary)

The cleanest place to skip is right where `pluginsToSetup` is assembled. Skipped plugin IDs go into a parallel `skippedIds` set that the done summary reads; `_enterCurrentPlugin` stays untouched.

- [ ] **Step 1: Add the bucket-tracking properties**

In `packages/web-ui/dist/static/onboarding.js`, in the `done state` block (around line 67-68), change from:

```js
    // --- done state ---
    enabledCount: 0,
```

to:

```js
    // --- done state ---
    // The walk-completion counters drive the three-bucket summary on the
    // done screen so the user can see what actually changed this trip.
    // 'newly enabled' = walked-to-completion plugins NOT in wasEnabledAtStart.
    // 'reconfigured'  = walked-to-completion plugins IN wasEnabledAtStart
    //                   (only possible via the Reconfigure opt-in).
    // 'left alone'    = pre-checked plugins skipped at startConfigure time.
    newlyEnabledCount: 0,
    reconfiguredCount: 0,
    leftAloneCount: 0,
```

(Note: we're removing `enabledCount`. Task 4 updates the done template references.)

- [ ] **Step 2: Modify `startConfigure` to partition plugins and skip the already-set-up ones**

Find `startConfigure()` (around line 265). Replace the whole method with:

```js
    async startConfigure() {
      if (this.selectedIds.size === 0) {
        // User picked nothing — go straight to done with all-zero buckets.
        this.newlyEnabledCount = 0;
        this.reconfiguredCount = 0;
        this.leftAloneCount = 0;
        this.phase = "done";
        return;
      }

      // Partition selectedIds into "needs the wizard" and "leave alone."
      // A plugin only enters the walk if it's new OR the user flipped
      // Reconfigure on for it. Everything else is in wasEnabledAtStart
      // and stays enabled with its existing config untouched.
      const toWalk = [];
      let skipCount = 0;
      for (const id of this.selectedIds) {
        if (this.wasEnabledAtStart.has(id) && !this.reconfigureIds.has(id)) {
          skipCount += 1;
        } else {
          toWalk.push(id);
        }
      }
      this.leftAloneCount = skipCount;
      this.newlyEnabledCount = 0;
      this.reconfiguredCount = 0;

      if (toWalk.length === 0) {
        // Nothing to walk — pre-checks with no Reconfigure flips. Go
        // straight to the done summary so the user sees the
        // "left alone" count and understands nothing changed.
        this.phase = "done";
        return;
      }

      // Fetch contracts for the walk-needing plugins only. Skipping the
      // /contract fetch for left-alone plugins isn't just an optimization:
      // a stale contract endpoint shouldn't break a re-run that doesn't
      // touch that plugin.
      const contracts = [];
      for (const id of toWalk) {
        try {
          const contract = await api(`/api/plugin/${id}/contract`);
          contracts.push(contract);
        } catch (e) {
          console.warn(`Could not fetch contract for ${id}:`, e);
          const stub = this.allPlugins.find(p => p.id === id) || { id, name: id };
          contracts.push({ id, name: stub.name, setup_steps: [], required_settings: [], required_credentials: [] });
        }
      }

      this.pluginsToSetup = contracts;
      this.currentSetupIndex = 0;
      this._enterCurrentPlugin();
      this.phase = "configure";
    },
```

- [ ] **Step 3: Update `_onPluginComplete` to bucket the completion**

Find `_onPluginComplete()` (around line 331). Change from:

```js
    async _onPluginComplete() {
      this.enabledCount += 1;
      this._advancePluginOrFinish();
    },
```

to:

```js
    async _onPluginComplete() {
      // Bucket the just-completed plugin: was it new (newly set up) or
      // already-enabled-with-Reconfigure-on (reconfigured)? The
      // wasEnabledAtStart snapshot is the source of truth.
      const id = this.currentContract?.id;
      if (id && this.wasEnabledAtStart.has(id)) {
        this.reconfiguredCount += 1;
      } else {
        this.newlyEnabledCount += 1;
      }
      this._advancePluginOrFinish();
    },
```

- [ ] **Step 4: Hard-reload and verify the walk skip works**

Open `http://127.0.0.1:9292/`, trigger onboarding. With at least one plugin already enabled and Reconfigure OFF:

- Confirm clicking "Continue" jumps straight to `done` phase — no per-plugin wizard mounted.
- Console shows no errors.
- Refresh the daemon's status with no other change: re-running shows the same picker state.

Then flip Reconfigure ON for one already-enabled plugin and click "Set up 1 plugin." Confirm:

- The wizard walks that plugin's setup_steps.
- On completion, advances to `done`.

Then on a third re-run, un-check one already-enabled plugin and click "Continue." Confirm:

- Dashboard shows that plugin is STILL enabled (un-check did not disable).

- [ ] **Step 5: Commit**

```bash
git add packages/web-ui/dist/static/onboarding.js
git commit -m "feat(web-ui): skip already-set-up plugins in onboarding walk (SP6 task 3)

Re-onboarding's per-plugin configure walk now bypasses plugins the
user had already set up — unless they flipped Reconfigure on for that
row in the picker. The partition happens at startConfigure() time:
pluginsToSetup only contains the plugins that actually need the
wizard, and the rest are tracked in skippedIds (drives the done
screen's 'left alone' bucket in task 4).

Three counters replace the single enabledCount so the done summary
can distinguish 'newly set up' from 'reconfigured' from 'left alone'
— important so 'Set up 0 plugins this trip' on a casual re-run
doesn't look like a bug.

A user who pre-checks everything and Reconfigures nothing now sees
the picker advance straight to the done screen with all plugins in
the 'left alone' bucket and zero wizard mounts. Honors the user's
top-line feedback: 'if i redo onboarding, enabled plugins should
be pre-checked, and i shouldnt have to redo them.'

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Done-phase three-bucket summary

**Files:**

- Modify: `packages/web-ui/dist/index.html` (done phase template)
- Modify: `packages/web-ui/dist/static/onboarding.js` (delete the dead `enabledCount` references in any template-facing getters; surface a helpful `doneSummaryEmpty` boolean)

- [ ] **Step 1: Find the existing `done` phase template and current copy**

Open `packages/web-ui/dist/index.html`, find `<template x-if="phase === 'done'">`. Read the current block — likely 10-20 lines covering a heading, an `enabledCount` reference, and a "Go to dashboard" button.

- [ ] **Step 2: Add a `doneSummaryEmpty` getter to `onboarding()`**

In `packages/web-ui/dist/static/onboarding.js`, right after the `primaryActionLabel` getter from Task 2 (or at the end of the data object before the closing brace), add:

```js
    // True when every bucket is zero — i.e. the user un-checked everything
    // and walked nothing. We swap to a friendlier "your setup is unchanged"
    // message instead of showing three "0" rows.
    get doneSummaryEmpty() {
      return this.newlyEnabledCount === 0
        && this.reconfiguredCount === 0
        && this.leftAloneCount === 0;
    },
```

- [ ] **Step 3: Replace the `done` phase template body**

In `packages/web-ui/dist/index.html`, replace the body of `<template x-if="phase === 'done'">` (everything inside, not the template itself) with:

```html
          <div class="space-y-6">
            <div>
              <h2 class="text-2xl font-semibold mb-2">You're all set</h2>
              <template x-if="!doneSummaryEmpty">
                <p class="text-slate-600 text-sm">
                  Here's what changed this trip.
                </p>
              </template>
              <template x-if="doneSummaryEmpty">
                <p class="text-slate-600 text-sm">
                  No plugins changed this trip — your existing setup is
                  unchanged. You can manage plugins anytime from the
                  dashboard.
                </p>
              </template>
            </div>

            <template x-if="!doneSummaryEmpty">
              <div class="space-y-2">
                <template x-if="newlyEnabledCount > 0">
                  <div class="flex items-center gap-3 px-4 py-3 rounded-lg bg-violet-50 border border-violet-100">
                    <div class="text-violet-700 font-semibold text-lg"
                         x-text="newlyEnabledCount"></div>
                    <div class="text-sm text-slate-700">
                      Newly set up — these plugins started collecting data.
                    </div>
                  </div>
                </template>
                <template x-if="reconfiguredCount > 0">
                  <div class="flex items-center gap-3 px-4 py-3 rounded-lg bg-amber-50 border border-amber-100">
                    <div class="text-amber-700 font-semibold text-lg"
                         x-text="reconfiguredCount"></div>
                    <div class="text-sm text-slate-700">
                      Reconfigured — settings or credentials updated.
                    </div>
                  </div>
                </template>
                <template x-if="leftAloneCount > 0">
                  <div class="flex items-center gap-3 px-4 py-3 rounded-lg bg-slate-50 border border-slate-200">
                    <div class="text-slate-600 font-semibold text-lg"
                         x-text="leftAloneCount"></div>
                    <div class="text-sm text-slate-600">
                      Already set up, left alone — they keep running with
                      their existing config.
                    </div>
                  </div>
                </template>
              </div>
            </template>

            <button @click="completeDone()"
                    class="px-6 py-2.5 rounded bg-violet-600 text-white font-medium hover:bg-violet-700 transition-colors">
              Go to dashboard
            </button>
          </div>
```

- [ ] **Step 4: Delete the orphaned `enabledCount` property**

Search `packages/web-ui/dist/static/onboarding.js` and `packages/web-ui/dist/index.html` for any remaining `enabledCount` references. Remove the `enabledCount: 0,` line that Task 3 Step 1 already removed from the data block; double-check no template still reads it. If a template does, replace with `(newlyEnabledCount + reconfiguredCount)` — but the new done template should not need it.

Run:

```bash
grep -rn enabledCount packages/web-ui/
```

Expected: no hits, or only references inside historical comments. Fix any remaining live reference.

- [ ] **Step 5: Hard-reload and walk the three scenarios end-to-end**

Open `http://127.0.0.1:9292/`. For each scenario, trigger onboarding and observe the done screen:

1. **Pre-checks only, no Reconfigure.** Click "Continue." Done screen shows only the "Already set up, left alone: N" card. No bucket for newly / reconfigured.
2. **One Reconfigure flipped on, no new plugins.** Click "Set up 1 plugin." Walk through. Done shows "Reconfigured: 1" and "Already set up, left alone: N−1."
3. **Un-check everything, click "Skip for now."** Done shows the "No plugins changed this trip — your existing setup is unchanged." friendly empty state.

- [ ] **Step 6: Commit**

```bash
git add packages/web-ui/dist/index.html packages/web-ui/dist/static/onboarding.js
git commit -m "feat(web-ui): three-bucket done summary on re-onboarding (SP6 task 4)

The done screen now shows three labelled cards — Newly set up,
Reconfigured, Already set up + left alone — instead of a single
'enabled N plugins' counter that misrepresented re-runs as 'set up 0
plugins.' Empty-state copy ('No plugins changed this trip — your
existing setup is unchanged') handles the case where the user
un-checked everything, so the screen never looks broken.

Removes the orphan enabledCount property that the new counters
replaced.

Closes the SP6 work the user asked for: re-running onboarding now
pre-checks already-enabled plugins, skips their wizards by default,
gives a Reconfigure escape hatch, and presents an honest summary of
what changed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Final code review

After Task 4 lands, dispatch a code-quality reviewer over the SP6 diff (`git diff $(git merge-base HEAD main)..HEAD -- packages/web-ui/`). Focus areas:

- Are the four state sets always kept consistent? (e.g. `wasEnabledAtStart` is only ever written in `_loadPlugins`; `reconfigureIds` membership ⊆ `wasEnabledAtStart` membership at all times.)
- No accidental shared-reference mutation? (every set write goes through `new Set(...)` to keep Alpine reactivity firing.)
- Does the done summary still make sense if `startConfigure()` is called twice in a row? (the answer should be: yes — `pluginsToSetup` / counters reset each time.)
- Is there any user path that calls `togglePlugin` without going through `_loadPlugins` first? (`addPlugin` deep-link, for example.) If so, `wasEnabledAtStart` may be stale or empty there — verify behavior and document.

If the reviewer flags a real issue, fix in-line as part of the SP6 commit chain. If it flags style noise only, ignore.

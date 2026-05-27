# SP6 — Re-onboarding: pre-check enabled plugins + whole-plugin skip

**Status:** spec ready for implementation plan
**Date:** 2026-05-27
**Scope:** web-ui only; no daemon changes

## Problem

Re-running onboarding today forces the user to walk back through every previously-configured plugin: check the box again, enter credentials again, pick the definition again, upload the takeout again. That's a strong disincentive to ever use the wizard for tweaks — and it means a user who hit "Run setup wizard" by mistake (or to add one new plugin) sits through the same setup ten times.

User feedback verbatim: "if i redo onboarding, enabled plugins should be pre-checked, and i shouldnt have to redo them."

## Goals

1. The plugin picker on a re-run pre-checks every plugin whose daemon state says `enabled === true`.
2. Pre-checked + already-enabled plugins are **not** walked through the per-plugin wizard by default. The walk passes right over them.
3. The user gets an escape hatch — a per-row "Reconfigure" toggle — for the case where they *do* want to re-walk a plugin (rotate a credential, change a definition).
4. Un-checking a pre-checked plugin removes it from this trip's walk but **does not** disable the plugin. Disable lives on the dashboard.

## Non-goals

- Per-step skip within a single plugin's wizard. (User explicitly chose whole-plugin skip when asked.)
- Disabling plugins from the picker. (User explicitly chose the cleaner separation.)
- Changing the `addPlugin()` entry path or the dashboard's per-plugin Configure button. Both already work.
- Any daemon-side change. `enabled` is already in `/api/status`; `POST /api/plugin/{id}/enable` is already idempotent.

## Design

### Data model in the wizard (`onboarding.js`)

Three sets, snapshotted at boot and mutated as the user clicks:

```js
selectedIds: Set<string>        // seeded from plugins.filter(p => p.enabled).map(p => p.id)
reconfigureIds: Set<string>     // initially empty; user opts in per row
wasEnabledAtStart: Set<string>  // immutable snapshot; same membership as the initial selectedIds
```

`wasEnabledAtStart` is the source of truth for "should this plugin's wizard be skipped." `selectedIds` mutates as the user checks/un-checks rows. The two are deliberately separate so that, e.g., un-checking and re-checking a row doesn't lose the "this was already enabled" information.

### Picker UI (Alpine `x-template` for `pick_plugins` in `packages/web-ui/dist/index.html`)

The pick_plugins phase is rendered by Alpine.js directly in `index.html` (the `<template x-if="phase === 'pick_plugins'">` block around line 362), not via a Lit component. Lit components live in the per-plugin wizard (one per `setup_step.kind`) and aren't touched by this work.

For each plugin row, two visual cases:

- **Already-enabled plugin** (`wasEnabledAtStart.has(plugin.id)`):
  - Checkbox is pre-checked.
  - Small "Reconfigure" toggle appears to the right of the row description. Default off.
  - Subtle "Set up" or similar pill so the user can tell at a glance.
- **New plugin**:
  - Standard unchecked checkbox.
  - No Reconfigure toggle (it's meaningless — there's nothing to reconfigure).

Subheader copy (above the list) makes the model legible: "Already-set-up plugins are pre-checked. Flip 'Reconfigure' if you want to walk through one again to change its settings."

### Configure-walk skip (`_enterCurrentPlugin` in `onboarding.js`)

Before the wizard mounts:

```js
const id = this.pluginsToSetup[this.currentSetupIndex];
if (this.wasEnabledAtStart.has(id) && !this.reconfigureIds.has(id)) {
  // Skip: no wizard mount, no /enable POST (already enabled).
  return this._advancePluginOrFinish({ skipped: true });
}
// Else: walk as today (mount wizard, then /enable POST on complete).
```

The `/enable` POST is also skipped on the skipped path. It's idempotent on the daemon side so calling it would be safe — but skipping it makes the intent visible in network logs and avoids a needless round-trip.

### `done` phase summary

Today the `done` screen says something like "All set!" Replace with three buckets so the user understands what just happened, especially when "0 plugins set up this trip" is the expected outcome of a re-run with no Reconfigure flips:

- **Newly set up: N** — plugins not in `wasEnabledAtStart` that were checked and walked through to completion.
- **Reconfigured: N** — plugins in `wasEnabledAtStart` that the user flipped Reconfigure on and walked through.
- **Already set up, left alone: N** — plugins in `wasEnabledAtStart` that were left checked but not Reconfigured (and were therefore skipped).

If all three are zero (user un-checked everything), say "No plugins changed this trip — your existing setup is unchanged."

### Edge cases handled by the design

- **`addPlugin()` skip-to-pick-plugins entry path:** Seeding runs from the same `/api/status` payload, so already-enabled plugins are still pre-checked even when the user only wanted to add one new plugin. They un-check the unwanted ones (no daemon effect) or just leave them — both paths arrive at "only the newly-checked plugin gets walked."
- **Plugin enabled but failing:** Skip applies. The dashboard's per-row Configure button (SP4) and the picker's Reconfigure toggle are both routes to fix it. The picker won't be the only path.
- **User toggles Reconfigure ON and then un-checks the row:** Un-check is canonical "not touching this trip" — both `selectedIds.delete(id)` and `reconfigureIds.delete(id)` happen together, so the state stays consistent.
- **User re-checks a row they un-checked:** It rejoins `selectedIds`. Reconfigure stays off. Symmetric with the initial pre-check behavior.

### What does not change

- The daemon. `enabled` is already in the plugin status; `/api/plugin/{id}/enable` is already idempotent.
- The `welcome` → `signin` → `collect_modes` → `pick_plugins` phase order (SP3 added `collect_modes`; SP6 keeps it).
- The per-plugin wizard internals (the Lit step components).
- The dashboard's Configure / Disable / Run now buttons (SP4).

## Testing

`packages/web-ui/` has no JS unit-test harness today — the existing onboarding logic is exercised via the backend's pytest suite (which hits the daemon's HTTP API directly) plus a manual browser walkthrough. SP6 keeps the same model:

- **Backend coverage already in place** — `/api/status` returning `enabled` per plugin, `/api/plugin/{id}/enable` being idempotent, and the dashboard's Configure / Disable routes all have existing pytest coverage. The SP6 change is purely a JS-side state-machine refinement on top of those endpoints; no new backend tests required.
- **JS-side verification** — done via the manual walkthrough below. If we hit a regression class that wants a regression test, that's the trigger to stand up a vitest harness for `static/onboarding.js`; SP6 doesn't introduce one preemptively (YAGNI).

Manual walkthrough (post-merge, on the dev daemon):

1. Sign in, set up 2-3 plugins normally via the wizard.
2. Open dashboard → "Run setup wizard."
3. Confirm picker shows those plugins pre-checked with "Set up" pill + Reconfigure toggle off.
4. Click Next. Wizard advances to `done` immediately. Summary reads "Already set up, left alone: N."
5. Re-run wizard. Flip Reconfigure on one plugin. Click Next. Wizard walks that plugin only. Done summary reads "Reconfigured: 1, Already set up, left alone: N−1."
6. Re-run wizard. Un-check one of the pre-checked plugins. Click Next. Skipped — but plugin remains enabled on the dashboard (no destructive side effect).
7. Re-run wizard, but this time also check a *new* plugin. Walk it. Done summary reads "Newly set up: 1, Already set up, left alone: N."

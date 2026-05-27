# Refactor 2: Setup-step Lit components — Execution Plan

**Task:** #68
**Scoping doc:** `docs/plans/2026-05-26-refactor-2-setup-step-components.md` (Option A locked in)
**Strategy:** Lit 3 from CDN, no build step, light-DOM components, one component per `SetupStep.kind`, dispatched by a `<fulcra-step>` element. Wire format unchanged; daemon untouched.

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. Each task is 2–5 min. Commit at the end of every phase (or per-component if convenient).

## Phases

| Phase | Scope | Tasks |
|---|---|---|
| **1** | CDN integration + `<fulcra-step>` dispatcher skeleton + `<fulcra-step-intro>` proof component, wired into both render sites with visual parity verified | 6 |
| **2** | Port the remaining 10 step components | 10 |
| **3** | Strip duplicated inline templates from `index.html`, run final `node --check`, full pytest sweep, commit | 4 |
| **4** | Documentation pass (parallel subagent) | 1 |
| **Total** | | **21** |

## Step kinds to port (canonical set, from `packages/collect/fulcra_collect/plugin.py:103-107`)

1. `intro` — proof component (Phase 1)
2. `external_action`
3. `input` (most complex — field-kind sub-switch for `enum` / `toggle` / `password` / `secret` / `url` / `port` / text default)
4. `oauth`
5. `file_upload`
6. `permission_request`
7. `browser_extension`
8. `extension_pair`
9. `test_connection`
10. `definition_picker` (largest — ~150 lines, the worst drift offender)
11. `done`

## Risks locked in

- **Light DOM only.** Every component overrides `createRenderRoot() { return this; }` so Alpine selectors and `x-data` ancestors continue to see the rendered nodes. This is the #1 risk from the scoping doc; not negotiable.
- **No reactive prop binding via Alpine `:prop`.** Alpine cannot set DOM properties (only attributes). Render sites use `x-effect` to imperatively set `.step` / `.ctx` on the custom element when state changes — see Task 1.3.
- **No JS test runner introduced.** Verification = `node --check` for syntax + manual visual walkthrough + the existing `pytest` suite to confirm daemon contract unchanged.
- **Phase 2 keeps inline templates alive.** Each new component is wired in *alongside* the existing inline `<template x-if="current_step.kind === '...'">` blocks — the dispatcher renders only when its component is registered; otherwise it stays empty and Alpine's existing block renders. Templates only get deleted in Phase 3 once all 11 components ship. This guarantees visual parity throughout the migration.

  Concrete dispatch mechanism: `<fulcra-step>` exposes a static `Object.keys(window.FulcraStepComponents || {})` set; if `step.kind` is in the set, it renders the matching component, otherwise it renders nothing (returns `html``). Each component file registers itself: `window.FulcraStepComponents = { ...(window.FulcraStepComponents||{}), intro: 'fulcra-step-intro' };`. As we add components in Phase 2, the dispatcher takes over each kind one at a time without code changes.

---

## Phase 1 — CDN + dispatcher + intro proof (~2h)

### Task 1.1: Add Lit 3 CDN script tag with SRI

**File:** `packages/web-ui/dist/index.html` (lines 19–23, after the Alpine tag)

- [ ] **Step 1: Resolve the SRI hash.** From a terminal:
```bash
curl -s https://cdn.jsdelivr.net/npm/lit@3.2.1/index.js | openssl dgst -sha384 -binary | openssl base64 -A
```
Use the exact version you fetched (3.2.x latest as of 2026-05). **Note:** Lit's CDN entry is an ES-module bundle; the script tag must use `type="module"`. Note this differs from marked/alpine (which are classic scripts) — call it out in the comment.

- [ ] **Step 2: Insert the script tag** immediately after the Alpine block:
```html
  <!-- Lit 3 — web-components base used by the setup_step renderer
       (refactor #68). Pinned + SRI'd exactly like marked + alpine.
       Note: Lit ships as an ES module, so type="module" (not classic).
       Upgrade dance: bump version, recompute the hash with
         curl -s https://cdn.jsdelivr.net/npm/lit@<new>/index.js \
           | openssl dgst -sha384 -binary | openssl base64 -A
       and update both src and integrity below. -->
  <script type="module"
          src="https://cdn.jsdelivr.net/npm/lit@3.2.1/index.js"
          integrity="sha384-<HASH-FROM-STEP-1>"
          crossorigin="anonymous"></script>
```

- [ ] **Step 3: Verify.** Open the app in a browser, open devtools console, type `await import('https://cdn.jsdelivr.net/npm/lit@3.2.1/index.js')` — should resolve to an object exposing `LitElement` and `html`. If SRI hash is wrong the browser refuses with a console error; that's the signal to recompute.

### Task 1.2: Create the components directory and a tiny shared base

**Files to create:**
- `packages/web-ui/dist/static/components/_base.js`
- `packages/web-ui/dist/static/components/index.js` (registers all components by importing them)

- [ ] **Step 1:** Write `_base.js`:
```javascript
// packages/web-ui/dist/static/components/_base.js
//
// Shared base for every <fulcra-step-*> component.
//
// Why a shared base:
//   - light DOM is mandatory: Alpine ancestors must still see our rendered
//     nodes via their selectors. Shadow DOM would hide them.
//   - every component exposes the same prop shape (`step`, `ctx`) — see the
//     dispatcher contract below.
//   - every component registers itself in window.FulcraStepComponents so the
//     <fulcra-step> dispatcher can switch on step.kind without an explicit
//     import list.
//
// Component prop contract:
//   .step  — the current SetupStep object from plugin_contract.setup_steps.
//            Read-only from the component's perspective.
//   .ctx   — the wizard data object (the thing returned by createWizard()).
//            Components call methods on it (e.g. ctx.updateField(...)) and
//            read reactive-ish state (e.g. ctx.healthResult). Because the
//            same object instance is reused, identity comparisons work.
//
// Components do NOT mutate ctx; they call its methods. State lives in
// createWizard()'s closure exactly as today.
import { LitElement, html, nothing } from "https://cdn.jsdelivr.net/npm/lit@3.2.1/index.js";

export { html, nothing };

export class FulcraStepBase extends LitElement {
  static properties = {
    step: { type: Object },
    ctx:  { type: Object },
  };

  // Light DOM — see comment block above. Do not remove without rewriting
  // the Alpine integration in onboarding.js / dashboard.js. Tailwind class
  // names depend on this too: the global tailwind.css scan only sees light-
  // DOM nodes, so any class string used inside a shadow root wouldn't be
  // emitted.
  createRenderRoot() { return this; }
}

// Registry — components push themselves onto this so <fulcra-step> can
// route by step.kind. We do registration this way (instead of a static
// import map) to keep Phase 2 incremental: a kind without a registered
// component is simply not rendered by the dispatcher, and the existing
// Alpine inline template handles it. When all 11 are registered we delete
// the inline templates in Phase 3.
window.FulcraStepComponents = window.FulcraStepComponents || {};
```

- [ ] **Step 2:** Write `index.js` as the loader. For Phase 1 it imports the dispatcher + intro only:
```javascript
// packages/web-ui/dist/static/components/index.js
//
// Single entry point for the setup_step component bundle. Loaded as a
// module from index.html *after* lit and *before* the wizard.js / page
// scripts that mount the wizard. Order matters because:
//   - Lit must be loaded (the module already imports it).
//   - Components must be registered as custom elements before the first
//     <fulcra-step> appears in the DOM.
import "./step.js";          // the <fulcra-step> dispatcher
import "./step-intro.js";    // first ported component (Phase 1)
// Phase 2 will add:
// import "./step-external_action.js";
// import "./step-input.js";
// import "./step-oauth.js";
// import "./step-file_upload.js";
// import "./step-permission_request.js";
// import "./step-browser_extension.js";
// import "./step-extension_pair.js";
// import "./step-test_connection.js";
// import "./step-definition_picker.js";
// import "./step-done.js";
```

- [ ] **Step 3:** Wire it into `index.html` immediately *after* the Lit `<script type="module">` tag from Task 1.1 and *before* `/static/wizard.js`:
```html
  <script type="module" src="/static/components/index.js"></script>
```

- [ ] **Step 4:** `node --check packages/web-ui/dist/static/components/_base.js` and `node --check packages/web-ui/dist/static/components/index.js`. Both must exit 0. (`node --check` parses but doesn't execute imports — it catches syntax errors only, which is the gate we have.)

### Task 1.3: Build the `<fulcra-step>` dispatcher

**File to create:** `packages/web-ui/dist/static/components/step.js`

- [ ] **Step 1:** Write the dispatcher:
```javascript
// packages/web-ui/dist/static/components/step.js
//
// <fulcra-step> — routes to the kind-specific component by step.kind.
//
// Usage from a render site (light DOM, prop binding via x-effect because
// Alpine's `:prop` only sets attributes, not properties — and we want the
// .step / .ctx values to be the actual objects, not their toString()):
//
//   <fulcra-step x-effect="$el.step = current_step; $el.ctx = $data"></fulcra-step>
//
// Why x-effect: it re-runs when its dependencies change, so when
// current_step flips on Next/Back the new step object is written to .step
// and Lit's reactive update kicks in. $data is Alpine's reference to the
// current x-data scope (the createWizard() object) — same identity each
// time, so passing it as ctx is stable.
import { FulcraStepBase, html, nothing } from "./_base.js";

class FulcraStep extends FulcraStepBase {
  render() {
    if (!this.step) return nothing;
    const tag = window.FulcraStepComponents[this.step.kind];
    // No component registered for this kind yet — render nothing so the
    // existing Alpine inline <template> takes over. This is the Phase 2
    // incremental-migration hinge.
    if (!tag) return nothing;
    // We can't use a dynamic tag in Lit's tagged-template (it expects a
    // literal). Use document.createElement + unsafeStatic alternative:
    // build the element imperatively and let Lit's child rendering accept
    // it. The simplest path is to create the element once and update its
    // properties on each render.
    const el = document.createElement(tag);
    el.step = this.step;
    el.ctx  = this.ctx;
    return el;
  }
}
customElements.define("fulcra-step", FulcraStep);
```

- [ ] **Step 2:** `node --check packages/web-ui/dist/static/components/step.js`. Exit 0.

### Task 1.4: Build the `<fulcra-step-intro>` proof component

**File to create:** `packages/web-ui/dist/static/components/step-intro.js`

- [ ] **Step 1:** Match the existing inline template at `index.html:295-298`. It's the simplest — just renders `body_html` from the wizard ctx (already-rendered markdown via `marked`).
```javascript
// packages/web-ui/dist/static/components/step-intro.js
//
// kind="intro" — introductory text page, no user input.
//
// Replaces the inline template at index.html ~line 295 (onboarding flow)
// and ~line 1047 (dashboard Configure flow). Visual contract:
//   <div class="prose prose-sm text-slate-700 max-w-none"
//        x-html="body_html"></div>
//
// body_html is wizard.js's pre-rendered markdown — see renderMd() in
// wizard.js. We unsafeHTML it because the daemon already produced safe
// HTML (marked + sanitize-passthrough on trusted plugin metadata).
import { FulcraStepBase, html } from "./_base.js";
import { unsafeHTML } from "https://cdn.jsdelivr.net/npm/lit@3.2.1/directives/unsafe-html.js";

class FulcraStepIntro extends FulcraStepBase {
  render() {
    const bodyHtml = this.ctx?.body_html || "";
    return html`
      <div class="prose prose-sm text-slate-700 max-w-none">
        ${unsafeHTML(bodyHtml)}
      </div>
    `;
  }
}
customElements.define("fulcra-step-intro", FulcraStepIntro);
window.FulcraStepComponents.intro = "fulcra-step-intro";
```

- [ ] **Step 2:** If you added a new SRI-pinned URL for the unsafe-html directive, **compute its hash too** and decide policy: either pin via importmap, OR (simpler and what we'll do) treat directive submodules as trusted-by-the-lit-package SRI surface — Lit's main `index.js` is SRI-pinned, and the directive subpath is loaded as a same-origin module from the same versioned package. Document this rationale in the comment.

- [ ] **Step 3:** `node --check packages/web-ui/dist/static/components/step-intro.js`. Exit 0.

### Task 1.5: Wire the dispatcher into both render sites

**File:** `packages/web-ui/dist/index.html`

- [ ] **Step 1:** Site A — onboarding flow. Insert this **immediately before** the existing `<!-- ---- intro ---- -->` template (line ~294):
```html
                <!-- Lit dispatcher (refactor #68 — Phase 1). Renders
                     whichever kind has a registered <fulcra-step-*>
                     component; falls through to the legacy inline
                     templates below for kinds not yet ported. The
                     x-effect writes properties (not attributes) so the
                     component receives real objects. -->
                <fulcra-step x-effect="$el.step = current_step; $el.ctx = $data"></fulcra-step>
```

- [ ] **Step 2:** Site B — dashboard Configure flow. Insert immediately before the `<!-- intro -->` template at line ~1046, same line, same comment.

- [ ] **Step 3:** Ensure the legacy inline `intro` templates at lines ~294-298 and ~1047-1049 are **still in place** — they stay until Phase 3.

  But: when the dispatcher renders something, both it and the legacy template would render the intro body simultaneously. To prevent the double render during Phase 1/2, gate the legacy template with `x-if="!window.FulcraStepComponents['<kind>']"`. For Phase 1, the intro line becomes:
  ```html
                  <template x-if="current_step.kind === 'intro' && !(window.FulcraStepComponents && window.FulcraStepComponents.intro)">
  ```
  Do this for both sites' intro templates only (other kinds keep their existing condition until their component lands in Phase 2; the dispatcher returns `nothing` for them, so no double-render risk).

### Task 1.6: Visual parity verification + commit

- [ ] **Step 1:** Build/run the daemon. Open `/` in a browser, walk through a plugin whose first step is `intro` (Generic RSS works — `packages/collect/fulcra_collect/plugins/generic_rss.py`). Confirm the intro page renders identically to the old version (font, prose styling, body content). The DOM should now have a `<fulcra-step>` wrapper holding a `<fulcra-step-intro>` child.

- [ ] **Step 2:** Same check via the dashboard Configure flow. Click "Configure" on an existing plugin whose wizard starts with intro.

- [ ] **Step 3:** Open devtools, inspect `document.querySelector('fulcra-step-intro')`. Confirm its parent's Alpine `$data` is reachable from the component (open console: `document.querySelector('fulcra-step-intro').ctx` should return the wizard data object). This is the smoke test for the light-DOM + Alpine interop.

- [ ] **Step 4:** Run `node --check` on every touched JS file:
```bash
node --check packages/web-ui/dist/static/components/_base.js
node --check packages/web-ui/dist/static/components/index.js
node --check packages/web-ui/dist/static/components/step.js
node --check packages/web-ui/dist/static/components/step-intro.js
```

- [ ] **Step 5:** Run the daemon's pytest suite to confirm we didn't accidentally touch the contract:
```bash
cd packages/collect && uv run pytest -x
```

- [ ] **Step 6:** Commit:
```
feat(web-ui): add Lit 3 CDN + <fulcra-step> dispatcher + intro component (refactor #68 phase 1)

Land the foundation for the setup_step component model:
- Lit 3.2.1 pinned via SRI in index.html (same pattern as marked/alpine)
- Light-DOM base class so Alpine selectors still resolve into our components
- <fulcra-step> dispatcher routes by step.kind via a window registry, so
  the migration can land one kind at a time without flipping a master switch
- <fulcra-step-intro> as the first ported kind, with double-render guarded
  by the legacy inline template's x-if condition

Visual parity verified manually for both onboarding and dashboard-Configure
flows on a Generic RSS plugin walkthrough. Daemon contract unchanged (no
changes under packages/collect/); pytest suite passes.

Refs #68.
```

---

## Phase 2 — port the remaining 10 components (~3h)

For every task below: write the component, add its `import "./step-<kind>.js";` line to `components/index.js`, gate the legacy template's `x-if` condition with `&& !(window.FulcraStepComponents && window.FulcraStepComponents['<kind>'])` at **both** render sites, `node --check`, manual visual walkthrough, commit.

The component file is the mechanical translation of the inline template — same Tailwind classes, same conditional branches, but `@click` becomes `@click=${...}`, `x-text` becomes `${...}`, `x-show` becomes a `?hidden=` or a ternary returning `nothing`, etc.

> **Component bootstrap template.** For each kind, start from this skeleton and fill in the `render()` body by translating the inline `<template x-if>` block at `index.html:<line>`:
>
> ```javascript
> // packages/web-ui/dist/static/components/step-<KIND>.js
> //
> // kind="<KIND>" — <one-line summary copied from SetupStep docstring>.
> // Mirrors index.html ~line <LINENO> (onboarding) and ~line <LINENO> (dashboard).
> import { FulcraStepBase, html, nothing } from "./_base.js";
> import { unsafeHTML } from "https://cdn.jsdelivr.net/npm/lit@3.2.1/directives/unsafe-html.js";
>
> class FulcraStep<PascalKind> extends FulcraStepBase {
>   render() {
>     const s = this.step, c = this.ctx;
>     // ... translate template here ...
>     return html`...`;
>   }
> }
> customElements.define("fulcra-step-<kind>", FulcraStep<PascalKind>);
> window.FulcraStepComponents["<kind>"] = "fulcra-step-<kind>";
> ```

### Task 2.1: `<fulcra-step-external_action>`

- [ ] **Step 1:** Translate `index.html:301-317`. Inputs from ctx: `body_html`, `step.external_link`. No event handlers.
- [ ] **Step 2:** Register in `components/index.js`.
- [ ] **Step 3:** Gate both legacy templates (`index.html:301` and `~1052`) with the `!window.FulcraStepComponents['external_action']` guard.
- [ ] **Step 4:** `node --check` + manual visual check using any plugin that has an external_action step (e.g. Trakt — visit URL to create OAuth app).
- [ ] **Step 5:** Commit: `feat(web-ui): port external_action step to <fulcra-step-external_action> (refactor #68)`.

### Task 2.2: `<fulcra-step-input>`

Most complex of the field-rendering kinds — has a nested switch on `field.kind` (`enum`, `toggle`, `password`/`secret`, default text/url/port). The list of fields comes from `ctx.input_fields`; each `updateField(key, value)` call hits `ctx.updateField`.

- [ ] **Step 1:** Translate `index.html:320-374`. Drive the field list from `c.input_fields`; for each field render one of four sub-branches. Note the value-vs-checked binding nuances (the toggle one is tricky — accepts boolean OR string `"true"`).
- [ ] **Step 2:** Register + gate (both sites: `index.html:320` and `~1067`).
- [ ] **Step 3:** `node --check` + manual walkthrough with Generic RSS (it has at least one text input) AND a plugin with a password/secret field (Trakt: client_secret).
- [ ] **Step 4:** Commit: `feat(web-ui): port input step to <fulcra-step-input> (refactor #68)`.

### Task 2.3: `<fulcra-step-oauth>`

- [ ] **Step 1:** Translate `index.html:722-733`. Inputs: `body_html`, `ctx.oauthStatus`. Event: button click → `ctx.startOAuth()`.
- [ ] **Step 2:** Register + gate (`index.html:722` and `~1401`).
- [ ] **Step 3:** `node --check` + walkthrough with Trakt OAuth.
- [ ] **Step 4:** Commit: `feat(web-ui): port oauth step to <fulcra-step-oauth> (refactor #68)`.

### Task 2.4: `<fulcra-step-file_upload>`

- [ ] **Step 1:** Translate `index.html:377-413`. Inputs: `body_html`, `ctx.uploadedFileName`, `ctx.uploadInFlight`, `ctx.uploadProgress`. Event: `<input type="file">` change → `ctx.onFileChange(event)`. **Site B (line ~1119)** uses a *different* `id="setupFileUploadInput"` for the input — we standardise to a single unique id derived from the component instance (use `this._inputId ??= 'file-' + crypto.randomUUID()`) to keep the `<label for=...>` link working when both flows can be live in the DOM at once.
- [ ] **Step 2:** Register + gate.
- [ ] **Step 3:** `node --check` + walkthrough with Spotify-takeout flow (the file_upload step's original raison d'être).
- [ ] **Step 4:** Commit: `feat(web-ui): port file_upload step to <fulcra-step-file_upload> (refactor #68)`.

### Task 2.5: `<fulcra-step-permission_request>`

- [ ] **Step 1:** Translate `index.html:421-459`. Inputs: `body_html`, `ctx.current_permission_id`, `ctx.permissionResult`, `ctx.permissionChecking`, `ctx.plugin_contract.permission_check_available`. Events: `ctx.permissionDeepLink(id)` and `ctx.checkPermission()`.
- [ ] **Step 2:** Register + gate.
- [ ] **Step 3:** `node --check` + walkthrough with a plugin that has a `permission_request` step (Apple Health / Photos).
- [ ] **Step 4:** Commit: `feat(web-ui): port permission_request step to <fulcra-step-permission_request> (refactor #68)`.

### Task 2.6: `<fulcra-step-browser_extension>`

- [ ] **Step 1:** Translate `index.html:462-483`. Inputs: `body_html`, `step.extension_url`, `ctx.extensionConfirmed`. Event: checkbox → `ctx.confirmExtension()`.
- [ ] **Step 2:** Register + gate.
- [ ] **Step 3:** `node --check` + walkthrough with Fulcra Attention extension plugin (the canonical user of this kind).
- [ ] **Step 4:** Commit: `feat(web-ui): port browser_extension step to <fulcra-step-browser_extension> (refactor #68)`.

### Task 2.7: `<fulcra-step-extension_pair>`

- [ ] **Step 1:** Translate `index.html:737-805`. Inputs: `body_html`, `ctx.pairStatus` (`idle`/`pairing`/`success`/`fallback`), `ctx.pairFallbackToken`, `ctx.pairManuallyConfirmed`. Events: `ctx.startExtensionPair()`, `ctx.copyPairToken()`, `ctx.confirmManualPair()`. Four-branch render based on `pairStatus`.
- [ ] **Step 2:** Register + gate.
- [ ] **Step 3:** `node --check` + walkthrough — pair the Fulcra Attention extension. **Test the 3-second fallback path** by temporarily disabling the extension to confirm the fallback UI still renders correctly under the component.
- [ ] **Step 4:** Commit: `feat(web-ui): port extension_pair step to <fulcra-step-extension_pair> (refactor #68)`.

### Task 2.8: `<fulcra-step-test_connection>`

- [ ] **Step 1:** Translate `index.html:486-565` (the richer onboarding-site version — site B at 1212 is a stripped-down variant). The component uses the **richer** version (with preview entries, subtitle, watched_at date), and Phase 3's deletion strips both. Inputs: `body_html`, `ctx.healthChecking`, `ctx.healthResult` (which has `ok`, `summary`, optional `preview[]`), `ctx.healthError`. Event: `ctx.runHealthCheck()`.
- [ ] **Step 2:** Register + gate.
- [ ] **Step 3:** `node --check` + walkthrough with Trakt (has a rich health_check result with watch preview).
- [ ] **Step 4:** Commit: `feat(web-ui): port test_connection step to <fulcra-step-test_connection> (refactor #68)`.

### Task 2.9: `<fulcra-step-definition_picker>`

The biggest component (~150 lines) and the original drift offender — site A (~568-719) vs site B (~1248-1399) explicitly diverged. The component file is the canonical source.

- [ ] **Step 1:** Translate the richer onboarding-site version (`index.html:568-719`). Inputs: `body_html`, `ctx.dpLoading`, `ctx.dpError`, `ctx.dpDefinitions`, `ctx.dpOtherDefinitions`, `ctx.dpSelectedId`, `ctx.dpShowOther`, `ctx.dpForceNew`, `ctx.dpNewName`, `ctx.plugin_contract.canonical_definition_name`. Helpers: `ctx.dpSelectDef(def)`, `ctx.dpEntryLabel(entry)`, `ctx.dpHumanDate(s)`, `ctx.dpChooseForceNew()`. Three sub-blocks: chosen-defs list, other-defs collapsible, create-new footer.
- [ ] **Step 2:** Register + gate (gate both `index.html:568` and `~1248`).
- [ ] **Step 3:** `node --check` + walkthrough — Generic RSS, then a Trakt re-onboard, then run the **dashboard Configure flow** on a plugin to verify site B no longer drifts (because there is no "site B" anymore — same component).
- [ ] **Step 4:** Commit: `feat(web-ui): port definition_picker to <fulcra-step-definition_picker> (refactor #68)`. In the commit body explicitly call out: "this is the kind that motivated the refactor — site A/B drift now structurally impossible."

### Task 2.10: `<fulcra-step-done>`

- [ ] **Step 1:** Translate `index.html:808-872`. Inputs: `step.title`, `body_html`, `ctx.firstRunStatus` (`idle`/`running`/`done`/`error`/`slow`), `ctx.firstRunSummary`, `ctx.firstRunTimelineUrl`. No events (the only interaction is the "view timeline" link). Note: this step normally has its own title rendered alongside the green checkmark — match the existing inline behaviour where the *outer* `<h2>` is suppressed for kind=done; the component renders its own title.
- [ ] **Step 2:** Register + gate.
- [ ] **Step 3:** `node --check` + walkthrough — finish a plugin onboarding to land on the done step. Cover all four states by triggering first-run errors deliberately (e.g. break credentials before completing).
- [ ] **Step 4:** Commit: `feat(web-ui): port done step to <fulcra-step-done> (refactor #68)`.

### End of Phase 2

At this point all 11 kinds have a registered component. Every render of the legacy inline templates is suppressed by its gating `x-if`. The dispatcher renders everything. The legacy templates are dead code — but still present.

---

## Phase 3 — strip duplicated templates, finalise, commit (~1h)

### Task 3.1: Remove all kind-specific inline templates at site A

**File:** `packages/web-ui/dist/index.html`

- [ ] **Step 1:** Delete the 11 inline `<template x-if="current_step.kind === '<kind>'">` blocks under the onboarding render site (`index.html` ~lines 295-872). Keep the `<fulcra-step>` line, the outer step-title block (line 290-292), the step-error template (875-877), and the Back/Next navigation (880+).

- [ ] **Step 2:** Visual check the file is still valid HTML — no orphaned closing `</template>` tags, no dangling comments referring to the deleted blocks.

### Task 3.2: Remove all kind-specific inline templates at site B

- [ ] **Step 1:** Delete the 11 inline `<template x-if=...>` blocks under the dashboard render site (`index.html` ~lines 1047-1500). Keep the dispatcher line and surrounding scaffolding.

- [ ] **Step 2:** Delete the "this block is intentionally a near-verbatim copy" comment block at ~line 1260-1268 — its premise no longer holds.

### Task 3.3: Simplify the dispatcher gates

- [ ] **Step 1:** Now that every kind is component-backed, simplify the `<fulcra-step>` lines: they were never gated, so no change there. But also: the `_base.js` comment about "until all 11 are registered" can be tightened — the registry no longer needs the "fallback to inline template" framing. Update the comment to reflect that all kinds are now component-only.

### Task 3.4: Final verification + commit

- [ ] **Step 1:** `node --check` every touched JS file:
```bash
for f in packages/web-ui/dist/static/components/*.js packages/web-ui/dist/static/{wizard,onboarding,dashboard,app,settings}.js; do
  echo "checking $f"; node --check "$f" || exit 1
done
```

- [ ] **Step 2:** Run the daemon's full pytest suite to confirm zero daemon-side regressions (the wire contract / SetupStep shape MUST be unchanged):
```bash
cd packages/collect && uv run pytest
```

- [ ] **Step 3:** End-to-end manual walkthrough covering at least three plugins exercising the full step variety:
  - Generic RSS — intro, input, definition_picker, done
  - Trakt — intro, external_action, input (with secret), oauth, test_connection, definition_picker, done
  - Fulcra Attention (browser extension) — intro, browser_extension, extension_pair, done

  Verify each kind from both the onboarding-flow site AND the dashboard-Configure site.

- [ ] **Step 4:** Pre-push orphan/obsolete sweep — re-read `index.html` and verify no comments referring to deleted templates remain ("near-verbatim copy", "if you change one change the other", etc.). Scan `wizard.js` for any rendering-specific helpers that are now unused. If found, delete in this commit.

- [ ] **Step 5:** Commit:
```
refactor(web-ui): delete duplicated setup_step templates from index.html (refactor #68 phase 3)

Now that every SetupStep kind has a registered <fulcra-step-*> component,
the inline <template x-if="current_step.kind === '...'"> blocks at both
render sites are dead code. Strip them. Visual + behavioural parity was
verified per-kind during phase 2; this commit only removes nodes that
were already being skipped at render time.

index.html shrinks by ~400 lines. Site A and site B no longer have any
duplicated rendering logic — the rendering for every step kind lives in
exactly one place: packages/web-ui/dist/static/components/step-<kind>.js.

The drift that motivated this refactor (definition_picker, #29 cousin
class of bug) is now structurally impossible.

Refs #68.
```

---

## Phase 4 — documentation (parallel subagent, runs after Phase 2 lands)

### Task 4.1: Write/refresh `packages/web-ui/README.md`

This task is dispatched to a **separate subagent in parallel** with Phase 3, per the user's "code documentation as I work (dispatch subagent for it)" preference. It does not gate Phase 3.

- [ ] **Step 1:** Read the current `packages/web-ui/README.md` (it exists — confirmed by `ls packages/web-ui/`). Capture what's stale.

- [ ] **Step 2:** Add a new top-level section **"Setup-step component model"** describing:
  - The Lit-via-CDN choice (rationale: no build step, real components, coexists with Alpine).
  - The light-DOM mandate and **why** (Alpine ancestor selectors must still resolve; Tailwind class scan only sees light DOM).
  - The component prop contract: `step` (current `SetupStep`), `ctx` (the wizard data object). Components read state from `ctx` and call methods on it; they do NOT mutate it directly.
  - The `<fulcra-step>` dispatcher and the `window.FulcraStepComponents` registry — explicitly call out that adding a new step kind is a 3-step process: (1) extend the Python `SetupStep` Literal in `packages/collect/fulcra_collect/plugin.py`, (2) write `packages/web-ui/dist/static/components/step-<kind>.js`, (3) add the import line to `components/index.js`.
  - The "no JS test runner" reality and what we use instead (`node --check` for syntax; pytest for daemon contract; manual visual walkthrough for behaviour).
  - The Phase 2 incremental migration pattern in case we ever need to do this kind of swap again.

- [ ] **Step 3:** Refresh any other section that referred to the inline `<template x-if=current_step.kind>` pattern as the rendering model.

- [ ] **Step 4:** Commit on the same branch as Phase 3 (or a fresh follow-up commit if Phase 3 already landed):
```
docs(web-ui): document the setup_step component model (refactor #68)

Adds a "Setup-step component model" section to packages/web-ui/README.md
explaining the Lit-via-CDN + light-DOM choice, the <fulcra-step>
dispatcher contract, and the recipe for adding a new step kind.

Refs #68.
```

---

## Risks & mitigations recap

| Risk | Mitigation |
|---|---|
| Alpine can't see DOM mutations from custom elements | Light DOM only (`createRenderRoot() { return this; }`). Confirmed in Phase 1 via devtools `$data` check. |
| Alpine `:prop` sets attributes not properties | Use `x-effect="$el.step = ...; $el.ctx = $data"` for object props. |
| Double-render during partial migration | Each legacy template gated with `!window.FulcraStepComponents['<kind>']`. Removed in Phase 3 when no longer needed. |
| Lit ES-module SRI subpath imports (unsafe-html directive) | Pinned via Lit's main package version; subpath is same-origin to the SRI-pinned root. Documented in `_base.js`. |
| First-run / health-check / extension-pair state machines breaking | All state stays in `createWizard()` closure (ctx); components are pure renderers. No state migrates into Lit reactive properties. Manual walkthrough per task includes the relevant state machine. |
| File-input `id="..."` collisions between site A and site B | Per-instance UUID id (`this._inputId ??= 'file-' + crypto.randomUUID()`). |

## What we are explicitly NOT doing

- No JS test runner. (The scoping doc filed this as a follow-up.)
- No Playwright smoke test. (Same.)
- No changes to the daemon's `SetupStep` dataclass or wire contract.
- No changes to `wizard.js`'s state machine (`_onStepEnter`, navigation, validation) — those are business logic, not rendering.
- No changes to `onboarding.js`, `dashboard.js`, `app.js`, `settings.js` beyond what's needed at the call site (and even there, only `index.html` changes — these JS files stay).

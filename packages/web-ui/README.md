# fulcra-collect web UI

Browser-based UI for Fulcra Collect. The daemon's HTTP server (in
packages/collect/fulcra_collect/web.py) serves these static files
from `dist/`.

> **First time here?** See [docs/TESTING.md](../../docs/TESTING.md) for
> the end-to-end walkthrough: install, start the daemon, paste your
> Fulcra token, and walk Trakt onboarding step by step.

Tech: vanilla HTML5 + CSS3 + JavaScript + Alpine.js (CDN) +
Tailwind CSS (CDN) + Lit 3 (CDN, web components). No build step.

## Local development

The daemon serves this dir automatically when started:

```
fulcra-collect daemon
```

The web URL is printed in the daemon's logs and written to
`~/.config/fulcra-collect/web-url`. Open it in your browser.

To live-edit: just save files in `dist/`. Reload the browser.

## Architecture pointers

- Frontend talks to the daemon via JSON at `/api/*` with a Bearer
  token (the daemon sets a `fulcra_token` cookie on the initial
  HTML load).
- The wizard renderer (in `wizard.js`, Phase C) walks each plugin's
  `setup_steps` array fetched from `/api/plugin/{id}/contract`.
- Step rendering itself lives in Lit web components under
  `dist/static/components/` — see [Setup-step component model](#setup-step-component-model)
  below.

## URL-param deep-links

The boot path in `dist/static/app.js` (`app.boot()`, around lines 95-133)
consumes a `?route=...` query param on first load so external launchers
can land the user on a specific screen rather than the dashboard
default. The handler was added in SP4 (2026-05-27) to back the
menubar's "?" docs button and per-plugin Configure button — both open
URLs of this shape via `subprocess.run(["open", ...])` — but nothing
restricts the contract to that producer; any external link (a docs
page, a notification, a future CLI) can deep-link the same way.

Supported routes:

| URL                              | Lands on |
|----------------------------------|----------|
| `/?route=docs`                   | In-app docs, default page (`how-do-i-get-my-data`) |
| `/?route=docs&page=NAME`         | In-app docs, specific page slug |
| `/?route=configure&plugin=ID`    | The wizard for plugin `ID` (same flow as the dashboard's Configure button) |
| `/?route=settings`               | Settings page |

The handler is **gated by `signedIn`**. Unauthenticated users hit the
onboarding/signin flow first and the param is cleared before auth
completes — meaning a fresh-install user clicking a deep-link will
sign in and then land on the dashboard, not the requested route. This
is a known limitation; revisit if/when there's a real-world need for
deferred deep-link resolution.

After consumption the handler calls
`history.replaceState({}, "", window.location.pathname)` to strip the
query string so a reload doesn't re-trigger the route. The wizard /
docs / settings flows then run as if the user had navigated to them
from the dashboard.

The producer side lives in `packages/menubar/` — see that package's
README ("Deep-linking into the web UI") for the URLs the menubar emits
today. If you change the contract here, update both.

## Setup-step component model

The wizard renders one of N kinds of setup step (`intro`, `input`,
`oauth`, `file_upload`, `permission_request`, `browser_extension`,
`test_connection`, `definition_picker`,
`external_action`, `done`). Up to refactor #68 (2026-05-27) those
kinds were rendered by `<template x-if="current_step.kind === '...'">`
blocks inline in `index.html` — duplicated at two render sites
(onboarding flow + dashboard Configure flow) which inevitably drifted.

After #68 each kind is a Lit 3 web component, registered into a window
registry, and routed by a `<fulcra-step>` dispatcher.

### Why Lit (not a build step, not Alpine partials)

- **No build step.** Lit ships as an ES module on jsdelivr; the
  `<script type="module">` tag in `index.html` pins it via SRI, the
  same security posture as marked / alpine. Local development stays
  zero-config: save a file, reload the browser.
- **Real components, not partials.** Alpine has no partial / include
  system, which is why duplication happened. Lit gives us a first-class
  component model that's a 1:1 substitution for the inline templates.
- **Coexists with Alpine.** The wizard's state machine
  (`_onStepEnter`, navigation, validation, OAuth callback handling)
  stays in `wizard.js`'s Alpine `x-data` object — Lit components are
  *pure renderers* of that state. They read from `ctx` and call
  methods on it, but never mutate state directly.

### Light DOM is mandatory

Every component overrides `createRenderRoot() { return this; }` so
its rendered nodes live in the light DOM. This is non-negotiable
because:

1. Alpine ancestor selectors (`x-data`, `$data`, etc.) must still
   resolve into our nodes. Shadow DOM would hide them.
2. Tailwind's class-scan only sees light-DOM nodes — class names
   used inside a shadow root wouldn't be emitted into the JIT
   stylesheet.

The shared `FulcraStepBase` in `_base.js` already does this; don't
override it.

### Component prop contract

Every `<fulcra-step-*>` component takes two properties:

| Prop  | Type             | What it is |
|-------|------------------|------------|
| `step` | `SetupStep` | The current step object from `plugin_contract.setup_steps`. Read-only from the component's perspective. |
| `ctx`  | wizard data object | The same object instance Alpine's `x-data="currentWizard"` is bound to — every method (`ctx.updateField`, `ctx.runHealthCheck`, etc.) and state field (`ctx.healthResult`, `ctx.dpDefinitions`, etc.) the component touches lives on it. |

Components **read** from `ctx` and **call methods** on it. They do
NOT replace fields directly — state mutations go through the
wizard's existing setters / methods so the Alpine reactivity stays
intact.

### The `<fulcra-step>` dispatcher

Both render sites in `index.html` use this single line:

```html
<fulcra-step x-effect="$el.step = current_step; $el.ctx = $data"></fulcra-step>
```

`x-effect` re-runs whenever `current_step` flips, writing `.step`
and `.ctx` as DOM *properties* (not attributes — Alpine's `:prop`
syntax can only set attributes, and we want real object identity).
The dispatcher then routes by `step.kind` via the
`window.FulcraStepComponents` registry; if no component is
registered for the current kind, the dispatcher renders nothing —
that's the forward-compat safety net for when the daemon ships a
new SetupStep kind to an older web-ui build.

### Adding a new step kind

Three steps:

1. **Daemon side.** Extend the `SetupStep` Literal in
   `packages/collect/fulcra_collect/plugin.py` and add any new fields
   needed in the dataclass. The wire contract changes here.
2. **Component side.** Write
   `packages/web-ui/dist/static/components/step-<kind>.js`. Use
   `step-intro.js` (simplest) or `step-input.js` (most complex) as
   the bootstrap template. The bottom of every component file MUST do
   both: `customElements.define(...)` AND
   `window.FulcraStepComponents.<kind> = "fulcra-step-<kind>";`.
3. **Loader.** Add an `import "./step-<kind>.js";` line to
   `components/index.js` so the file runs at startup.

No `index.html` change is needed — the dispatcher already routes
every kind via the registry.

### Verification (no JS test runner)

We don't ship a JS test runner. The verification surface for a
component change is:

- `node --check packages/web-ui/dist/static/components/step-<kind>.js`
  catches syntax errors.
- `cd packages/collect && uv run pytest` confirms the daemon
  contract (the shape of `SetupStep`) didn't break in a way the
  component depends on.
- Manual visual walkthrough — open `/` in a browser, walk a plugin
  whose wizard exercises the touched kind, in BOTH the onboarding
  flow (new plugin) and the dashboard-Configure flow (existing
  plugin re-config).

### File layout

```
dist/static/components/
├── _base.js                       — shared FulcraStepBase + window registry init
├── index.js                       — single entry, imports all components
├── step.js                        — <fulcra-step> dispatcher
├── step-intro.js                  — kind="intro"
├── step-external_action.js        — kind="external_action"
├── step-input.js                  — kind="input"
├── step-oauth.js                  — kind="oauth"
├── step-file_upload.js            — kind="file_upload"
├── step-permission_request.js     — kind="permission_request"
├── step-browser_extension.js      — kind="browser_extension"
├── step-test_connection.js        — kind="test_connection"
├── step-definition_picker.js      — kind="definition_picker" (the original drift offender)
└── step-done.js                   — kind="done"
```

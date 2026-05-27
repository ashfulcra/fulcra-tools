# Refactor 2: Setup-step component model

**Task:** #68
**Priority:** medium (high pain but smaller blast radius than #67)
**Why now:** the wizard has 11+ setup-step `kind` values, the rendering for each lives in `index.html` (duplicated twice between onboarding flow and dashboard-Configure flow), and every new step kind we add requires changes in 3 places. The duplication WILL drift.

## Concrete pain

- `SetupStep.kind` enum has at least: `intro`, `input`, `oauth`, `permission_request`, `test_connection`, `file_upload`, `definition_picker`, `external_action`, `browser_extension`, `extension_pair`, `done`
- Each kind's template is implemented in `packages/web-ui/dist/index.html` — **twice** (lines ~542-682 for the onboarding-flow render site, lines ~1162-1259 for the dashboard-Configure render site)
- After multiple cycles of "land a fix in site A, forget site B" we now have a verbatim-copy comment on both sites flagging the duplication. It worked today; it won't forever.
- Adding a new kind (e.g. the conditional `input` step pattern in Plex cross-machine wizard #56) requires touching Python `SetupStep` dataclass + frontend renderer logic + both templates.

## Root cause

Alpine.js (our frontend framework) doesn't have **components**. We chose Alpine because it's CDN-loadable and requires no build step, which was the right call for the v0 wizard. But the wizard has now grown past Alpine's natural ceiling — Alpine is great for "decorate static HTML with reactivity"; it's wrong for "render a dynamic tree of step components."

## Option matrix

**Option A — Switch to a real component framework (Svelte / Preact / Lit)**
- Each setup_step kind becomes a real component with a defined prop contract
- Build step required, but Lit specifically can run without one (web components + tagged-template HTML)
- True deduplication: import the component once, use it from both render sites
- Significant rewrite of wizard.js + index.html
- Estimate: 1 multi-batch sprint

**Option B — Extract setup-step rendering into a single JS function returning a DOM tree**
- No framework change. wizard.js gains `renderStep(step, ctx)` that returns the DOM
- Both render sites call `renderStep` instead of having inline templates
- Templates in index.html collapse from 200 lines each to one `<div x-html="renderStep(current_step)">`
- Loses Alpine's reactivity inside the rendered step — would need manual DOM updates on state change (or use `x-effect` to re-render on dependency change)
- Estimate: 2-3 days of careful work

**Option C — Server-rendered step partials**
- Daemon's `/api/plugin/{id}/contract` includes pre-rendered HTML per step instead of (or alongside) the structured `kind` + fields
- Frontend just injects the HTML and wires up event handlers
- Trades flexibility for simplicity. Logic moves from JS to Python.
- Server-side rendering of Alpine-reactive content is awkward
- Estimate: 1-2 days but feels backwards

**Option D — Live with the duplication, add a contract test**
- Add a build-time check: parse index.html, find both `definition_picker` template blocks, assert they're byte-identical (minus indentation)
- CI failure when they drift
- Cheap; doesn't solve the underlying problem
- Estimate: 1 hour

## Recommendation: A (Lit web components, no build step)

Lit components define `class MyComponent extends LitElement` with tagged-template rendering. Use as custom elements: `<fulcra-step-definition-picker .step="${current_step}" @select="${handleSelect}"></fulcra-step-definition-picker>`.

Lit:
- Ships from CDN (we already do this with marked + alpinejs)
- No build step required
- Real components with prop validation
- Coexists with Alpine — we keep Alpine for the outer route shell and dashboard, replace ONLY the step rendering

This lets us keep the v1 frontend mostly intact (dashboard.js, settings.js, app.js, onboarding.js stay) and rewrite only the step-rendering surface — about 800 lines of wizard.js/index.html collapsing into ~12 small Lit components.

## Migration plan

1. Add lit@3 via CDN script tag (with SRI, same pattern as marked + alpine)
2. Build one component first as a proof — `<fulcra-step-intro>`. Use it from both render sites; verify visual + behavioral parity with the existing inline template.
3. Build the remaining 10 components, one per kind. Each is ~30-80 lines of Lit.
4. Strip the duplicated inline templates from index.html; both render sites become `<fulcra-step .step="${current_step}" @advance="${next}"></fulcra-step>` where `<fulcra-step>` dispatches to the right specific component by `step.kind`.
5. wizard.js shrinks substantially — the `_onStepEnter` per-kind branches stay (they're business logic, not rendering) but the kind-specific DOM logic moves into the components.

## Test strategy

We don't have a JS test runner today. Lit components can be tested with Web Test Runner or @open-wc/testing but that requires adding tooling. **For v1, treat the migration as "tested by visual diff" + the existing manual QA flow**. File "set up JS test runner for Lit components" as a follow-up.

Alternatively: keep node --check working as the syntax gate, add ONE Playwright smoke test that walks the full wizard for one plugin (Generic RSS) as the regression net.

## Risks

1. **Alpine ↔ Lit interop**: Alpine doesn't know about custom elements' DOM mutations. Have to confirm `<fulcra-step>` inside an Alpine `x-data` scope doesn't have shadow-DOM issues. Hold-the-shape: use light DOM (`createRenderRoot() { return this; }`) instead of shadow DOM in our components to keep Alpine selectors working.
2. **CDN supply chain**: Lit is from @lit/lit. Cdn.jsdelivr serves the npm packages we'd use. Same SRI pin model as marked.
3. **Browser compat**: Lit needs ES2019+ and custom-elements support. All current Mac browsers have it. Doesn't affect us — we ship to macOS users only.

## Time estimate

- Phase 1 (proof + framework setup + one component): 1 batch, ~2 hours
- Phase 2 (port remaining 10 components): 1 batch, ~3 hours
- Phase 3 (strip duplicated templates, wire up `<fulcra-step>` dispatcher): 1 batch, ~1 hour

Total: 1-2 focused sessions.

## Punt option

If we're not ready for the rewrite, **do Option D (contract test) right now as a 1-hour insurance policy**. It doesn't fix the duplication but it stops the drift. Could land tomorrow without committing to the bigger refactor.

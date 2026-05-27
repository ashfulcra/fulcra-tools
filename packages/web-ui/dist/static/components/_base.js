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
//
// SRI + Lit URL choice:
// We use `cdn.jsdelivr.net/gh/lit/dist@VER/all/lit-all.min.js` — the Lit
// team's pre-bundled CDN file — rather than `npm/lit@VER/index.js`, which
// uses bare module specifiers (`@lit/reactive-element`) that browsers
// can't resolve without an importmap. lit-all is one bundled file
// containing LitElement, html, nothing, AND every directive
// (`unsafeHTML`, etc.) as top-level exports. One SRI-pinned URL covers
// everything; no separate hashes per directive subpath.
// Discovered 2026-05-27: the npm/lit@VER/index.js URL silently 404'd
// every component because of the bare-specifier resolution failure.
import { LitElement, html, nothing } from "https://cdn.jsdelivr.net/gh/lit/dist@3.2.1/all/lit-all.min.js";

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
// route by step.kind without a static import map. To add a new kind:
// extend the Python SetupStep Literal, write step-<kind>.js, and add an
// import line to components/index.js so the file runs at startup. The
// dispatcher returns nothing for unregistered kinds — useful as a
// safety net when a new step kind ships from the daemon to an older
// web-ui build.
window.FulcraStepComponents = window.FulcraStepComponents || {};

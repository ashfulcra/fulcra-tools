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
// SRI policy for Lit directive subpaths (unsafe-html, etc.):
// Lit's main index.js is SRI-pinned at the script tag in index.html. The
// directive submodules (e.g. directives/unsafe-html.js) load as same-origin
// modules off the same versioned jsdelivr package — they ride the SRI
// integrity of the root package import rather than each carrying their
// own hash. If we ever drop CDN for a build step, both pinning policies
// collapse into the bundled-output hash.
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
// route by step.kind without a static import map. To add a new kind:
// extend the Python SetupStep Literal, write step-<kind>.js, and add an
// import line to components/index.js so the file runs at startup. The
// dispatcher returns nothing for unregistered kinds — useful as a
// safety net when a new step kind ships from the daemon to an older
// web-ui build.
window.FulcraStepComponents = window.FulcraStepComponents || {};

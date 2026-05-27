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
    // No component registered for this kind — render nothing. This is the
    // forward-compat safety net for the case where a new SetupStep kind
    // ships from the daemon to an older web-ui build that hasn't grown a
    // component for it yet.
    if (!tag) return nothing;
    // Lit's tagged-template expects a static tag name, not a dynamic one.
    // Build the child element imperatively and let Lit's child rendering
    // accept it as a node. The same element instance is reused across
    // renders because Lit's diff sees the same node identity from the
    // previous render — but here we recreate per-render for simplicity;
    // the child component's reactive update covers prop changes anyway.
    const el = document.createElement(tag);
    el.step = this.step;
    el.ctx  = this.ctx;
    return el;
  }
}
customElements.define("fulcra-step", FulcraStep);
